"""App web (Streamlit): playlists automáticas, historial propio y rangos personalizados.

Ejecutar:  streamlit run autoplaylist.py

Dos modos:
  * Nube (con DATABASE_URL): cada persona entra con su cuenta de Spotify (OAuth); su token
    se guarda cifrado en la BD y el cron horario registra también sus escuchas.
  * Local (sin DATABASE_URL): una sola cuenta, la autorizada con ``python auth.py`` (.cache),
    protegida opcionalmente con APP_PASSWORD.
"""
from __future__ import annotations

import datetime
import hmac
import html
import os
from dataclasses import dataclass

import streamlit as st

import charts
import history
import jobs
import smart
import stats
import tokens
import web_auth
from config import ConfigError, get_min_play_ms, get_timezone, history_enabled, use_db_tokens
from dateranges import MESES_ES, format_ts, month_label, range_for_dates, range_for_month, range_last_days
from history_import import import_export_bytes
from spotify_core import (
    AuthRequiredError,
    friendly_error,
    get_clean_track_uris,
    get_spotify_client,
    get_top_tracks,
)

MIN_YEAR = 2008  # Spotify no existía antes

CSS = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1150px; }
  .as-hero { padding: 2.5rem 0 1rem; }
  .as-hero h1 { font-size: 2.6rem; margin-bottom: .2rem; }
  .as-hero p { font-size: 1.1rem; opacity: .8; max-width: 38rem; }
  .as-kicker { text-transform: uppercase; letter-spacing: .12em; font-size: .75rem; opacity: .65; }
  .as-user { font-weight: 600; font-size: 1.05rem; }
</style>
"""


@dataclass
class Ctx:
    sp: object
    conn: object          # None si no hay historial (modo antiguo sin disco)
    user_id: str
    display_name: str
    tz: object


# ---------------------------------------------------------------- infraestructura

def _apply_streamlit_secrets() -> None:
    """Si existe .streamlit/secrets.toml (Streamlit Cloud), lo expone como variables de entorno."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key, value)
    except Exception:  # noqa: BLE001 - sin secrets.toml no hay nada que cargar
        pass


def _check_password() -> None:
    """Modo local: protege la app con APP_PASSWORD (el cliente actúa sobre TU cuenta)."""
    password = os.environ.get("APP_PASSWORD", "")
    if not password:
        return
    if st.session_state.get("auth_ok"):
        return
    typed = st.text_input("Contraseña de la app", type="password")
    if typed and hmac.compare_digest(typed.encode(), password.encode()):
        st.session_state["auth_ok"] = True
        st.rerun()
    if typed:
        st.error("Contraseña incorrecta.")
    st.stop()


@st.cache_resource
def _cached_file_client():
    """Modo local: token en el fichero .cache (python auth.py)."""
    return get_spotify_client()


def _session_conn():
    """Una conexión por sesión del navegador (SQLite en WAL o Postgres en la nube).

    En Postgres se comprueba en cada recarga por si el servidor la cortó.
    """
    conn = st.session_state.get("db_conn")
    if conn is None:
        conn = history.connect(check_same_thread=False)  # Streamlit re-ejecuta en otros hilos
        st.session_state["db_conn"] = conn
    return conn.ensure_alive()


def _show_outcome(outcome: jobs.JobOutcome) -> None:
    if outcome.ok:
        state = "creada" if outcome.result.created else "actualizada"
        st.success(f"**{outcome.name}**: {state} con {outcome.result.track_count} canciones. "
                   f"[Abrir en Spotify]({outcome.result.url})")
    else:
        st.warning(f"**{outcome.name}**: no se actualizó. {outcome.note}")


# ---------------------------------------------------------------- login (modo nube)

def _logout(forget: bool = False) -> None:
    if forget and st.session_state.get("user_id"):
        tokens.delete_token(st.session_state["db_conn"], st.session_state["user_id"])
    for key in ("user_id", "user_info", "sp"):
        st.session_state.pop(key, None)


def _handle_oauth_callback(conn) -> None:
    """Vuelta de Spotify con ?code=... (o ?error=...). Si todo va bien, inicia la sesión."""
    params = st.query_params
    if "error" in params:
        st.query_params.clear()
        st.warning("Has cancelado el inicio de sesión en Spotify.")
        return
    code = params.get("code")
    if not code:
        return
    try:
        result = web_auth.complete_login(conn, code, params.get("state", ""))
    except web_auth.LoginError as exc:
        st.query_params.clear()
        st.error(str(exc))
        return
    st.query_params.clear()
    st.session_state["user_id"] = result.user_id
    st.rerun()


def _login_page() -> None:
    st.markdown(
        '<div class="as-hero"><div class="as-kicker">autospoti</div>'
        "<h1>Tus playlists, a partir de lo que de verdad escuchas</h1>"
        "<p>Registra tus escuchas cada hora, crea playlists de cualquier periodo y mantén al día "
        "tus listas automáticas.</p></div>",
        unsafe_allow_html=True,
    )
    try:
        url = web_auth.authorize_url()
    except ConfigError as exc:
        st.error(str(exc))
        return
    st.link_button("Iniciar sesión con Spotify", url, type="primary", icon=":material/login:")
    st.caption("Se abre Spotify para que des permiso; al volver ya estarás dentro. "
               "Solo tienen acceso las cuentas autorizadas en la app.")


def _cloud_session(conn):
    """Devuelve (sp, user_info) del usuario con sesión iniciada; si no hay, muestra el login."""
    if not st.session_state.get("user_id"):
        _handle_oauth_callback(conn)
        _login_page()
        st.stop()
    user_id = st.session_state["user_id"]
    sp = st.session_state.get("sp")
    if sp is None:
        sp = get_spotify_client(cache_handler=tokens.DbCacheHandler(conn, user_id))
        st.session_state["sp"] = sp
    return sp


# ---------------------------------------------------------------- páginas

def page_home(ctx: Ctx) -> None:
    st.markdown('<div class="as-kicker">Inicio</div>', unsafe_allow_html=True)
    st.title(f"Hola, {ctx.display_name}")
    api_only = ctx.conn is None

    if not api_only:
        cov = history.coverage(ctx.conn, ctx.user_id)
        week = history.count_plays(ctx.conn, ctx.user_id, *range_last_days(7))
        c1, c2, c3 = st.columns(3)
        c1.metric("Escuchas registradas", f"{cov.total:,}".replace(",", "."))
        c2.metric("Últimos 7 días", f"{week:,}".replace(",", "."))
        c3.metric("Registro desde", format_ts(cov.first_ts, ctx.tz)[:10])
        if cov.total == 0:
            st.info("Aún no hay escuchas registradas. Importa tu historial completo en "
                    "**Importar historial**; las nuevas se registran solas cada hora.")

    with st.container(border=True):
        st.subheader("Playlists automáticas")
        if api_only:
            st.write(
                "- **Últimas reproducciones**: tus últimas 50 escuchas (no es un ranking).\n"
                "- **≈4 semanas y ≈6 meses**: rankings de Spotify."
            )
        else:
            st.write(
                "- **Últimas reproducciones**: tus últimas 50 escuchas (no es un ranking).\n"
                "- **Más escuchado (7 días)**: con tu registro propio.\n"
                "- **≈4 semanas y ≈6 meses**: rankings de Spotify (la API no ofrece 30/180 días exactos).\n"
                "- **Último año**: exacto si tu historial cubre 365 días; si no, el ranking de Spotify."
            )
        st.caption("Se actualizan solas cada día; aquí puedes forzarlo ahora.")
        label = "Actualizar las 3 playlists" if api_only else "Actualizar las 5 playlists"
        if st.button(label, type="primary", icon=":material/autorenew:"):
            try:
                with st.spinner("Procesando canciones..."):
                    outcomes = jobs.update_auto_playlists(ctx.sp, ctx.conn, ctx.user_id, api_only=api_only)
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_error(exc))
                return
            for outcome in outcomes:
                _show_outcome(outcome)


def page_import(ctx: Ctx) -> None:
    st.title("Importar historial")
    st.write(
        "Sube los archivos **Streaming_History_Audio_*.json** de tu *Extended streaming history* "
        "(Spotify > Privacidad > Descargar tus datos). Puedes subir varios a la vez; "
        "las repeticiones se ignoran, así que no pasa nada si importas el mismo archivo dos veces."
    )
    files = st.file_uploader("Archivos JSON", type="json", accept_multiple_files=True)
    if files and st.button("Importar", type="primary", icon=":material/upload:"):
        total = None
        with st.spinner("Importando..."):
            for uploaded in files:
                try:
                    stats = import_export_bytes(ctx.conn, ctx.user_id, uploaded.getvalue())
                except ValueError as exc:
                    st.error(f"{uploaded.name}: {exc}")
                    continue
                if total is None:
                    total = stats
                else:
                    total.merge(stats)
        if total is not None:
            st.success(f"{total.inserted} reproducciones nuevas ({total.duplicates} repetidas).")
            st.caption(
                f"Omitidas: {total.no_uri} sin URI (podcasts/archivos locales), {total.not_track} no son "
                f"canciones, {total.legacy_format} en formato antiguo, {total.invalid} inválidas."
            )
            if total.legacy_format:
                st.warning("Hay entradas en el formato antiguo (StreamingHistory*.json), que no incluye "
                           "URIs y no se puede usar. Pide el *Extended streaming history*.")
    cov = history.coverage(ctx.conn, ctx.user_id)
    st.info(f"Registro actual: {cov.total} reproducciones ({format_ts(cov.first_ts, ctx.tz)} → "
            f"{format_ts(cov.last_ts, ctx.tz)}).")


def _data_version(ctx: Ctx) -> tuple:
    """Cambia cuando hay escuchas nuevas: invalida las cachés de cálculo."""
    cov = history.coverage(ctx.conn, ctx.user_id)
    return cov.total, cov.last_ts


@st.cache_data(ttl=900, show_spinner="Calculando estadísticas...", max_entries=32)
def _cached_stats(_conn, _tz, user_id: str, tz_name: str, start_ts: int, end_ts: int,
                  min_ms: int, version: tuple):
    return stats.compute(history.fetch_plays(_conn, user_id, start_ts, end_ts), _tz, min_ms=min_ms)


@st.cache_data(ttl=900, show_spinner=False, max_entries=64)
def _cached_build(_conn, _tz, user_id: str, tz_name: str, spec_json: str, version: tuple):
    return smart.build_tracks(_conn, user_id, smart.Spec.from_json(spec_json), _tz)


def _split_names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _years(ctx: Ctx) -> list[int]:
    cov = history.coverage(ctx.conn, ctx.user_id)
    this_year = datetime.date.today().year
    first = datetime.datetime.fromtimestamp(cov.first_ts, ctx.tz).year if cov.first_ts else this_year
    return list(range(this_year, max(MIN_YEAR, first) - 1, -1))


def _playlist_actions(ctx: Ctx, name: str, spec: smart.Spec, tracks: list, key: str) -> None:
    """Botón crear/actualizar + opción de guardarla como receta (se recalcula cada día)."""
    keep = st.checkbox("Mantenerla actualizada cada día", key=f"{key}_keep",
                       help="Se guarda como receta en **Mis recetas**: cada día se recalcula con "
                            "estos mismos filtros y se actualiza la playlist.")
    if st.button("Crear / actualizar playlist", type="primary", icon=":material/playlist_add:",
                 disabled=not name.strip() or not tracks, key=f"{key}_go"):
        try:
            result = jobs.write_playlist(ctx.sp, ctx.conn, ctx.user_id, name.strip(),
                                         [t.uri for t in tracks], f"{spec.describe()} (autospoti).")
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
            return
        if keep:
            history.save_recipe(ctx.conn, ctx.user_id, name.strip(), spec.to_json())
        state = "creada" if result.created else "actualizada"
        extra = " Guardada en **Mis recetas**." if keep else ""
        st.success(f"Playlist '{name.strip()}' {state} con {result.track_count} canciones. "
                   f"[Abrir en Spotify]({result.url}){extra}")


def _tracks_table(tracks: list, last_seen: "dict[str, int] | None" = None, tz=None) -> None:
    rows = []
    for i, t in enumerate(tracks, 1):
        row = {"#": i, "Canción": t.name, "Artista": t.artist, "Escuchas": t.plays}
        if last_seen is not None:
            row["Última vez"] = format_ts(last_seen.get(t.uri), tz)[:10]
        rows.append(row)
    st.dataframe(rows, hide_index=True, width="stretch")


def page_custom(ctx: Ctx) -> None:
    st.title("Playlist personalizada")
    st.caption("Lo que más escuchaste según tus filtros (registro propio + historial importado). "
               f"Zona horaria: {ctx.tz}")
    today = datetime.date.today()
    mode = st.segmented_control("Periodo", ["Últimos días", "Un mes", "Fechas", "Todo"],
                                default="Últimos días") or "Últimos días"
    spec = smart.Spec(kind="top", min_seconds=get_min_play_ms() // 1000)

    if mode == "Últimos días":
        spec.range_type = "last_days"
        spec.days = int(st.number_input("Días", min_value=1, max_value=3650, value=30, step=1))
        label = f"últimos {spec.days} días"
    elif mode == "Un mes":
        col1, col2 = st.columns(2)
        month = col1.selectbox("Mes", range(1, 13), index=today.month - 1,
                               format_func=lambda m: MESES_ES[m - 1])
        year = col2.number_input("Año", min_value=MIN_YEAR, max_value=today.year, value=today.year, step=1)
        start_ts, end_ts = range_for_month(int(year), int(month), ctx.tz)
        last_day = datetime.datetime.fromtimestamp(end_ts - 1, ctx.tz).date()
        spec.range_type, spec.start, spec.end = "fixed", f"{int(year)}-{int(month):02d}-01", last_day.isoformat()
        label = month_label(int(year), int(month))
    elif mode == "Fechas":
        col1, col2 = st.columns(2)
        start = col1.date_input("Desde", value=today - datetime.timedelta(days=30),
                                min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        end = col2.date_input("Hasta (incluido)", value=today,
                              min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        if end < start:
            st.error("La fecha final es anterior a la inicial.")
            return
        spec.range_type, spec.start, spec.end = "fixed", start.isoformat(), end.isoformat()
        label = f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"
    else:
        spec.range_type = "all"
        label = "de siempre"

    with st.expander("Filtros avanzados", icon=":material/tune:"):
        if mode != "Un mes":
            spec.months = st.pills("Meses", list(range(1, 13)), selection_mode="multi",
                                   format_func=lambda m: MESES_ES[m - 1][:3],
                                   help="Ej.: junio, julio y agosto + «Todo» = lo que más escuchas en verano.") or []
        spec.weekdays = st.pills("Días de la semana", list(range(7)), selection_mode="multi",
                                 format_func=lambda d: smart.DIAS[d].capitalize()) or []
        franjas = st.pills("Franja horaria", list(smart.FRANJAS), selection_mode="multi",
                           format_func=lambda f: f"{f.capitalize()} ({smart.FRANJAS[f].start}-"
                                                 f"{smart.FRANJAS[f].stop}h)") or []
        spec.hours = sorted({h for f in franjas for h in smart.FRANJAS[f]})
        col1, col2 = st.columns(2)
        spec.include_artists = _split_names(col1.text_input("Solo estos artistas", placeholder="separados por comas"))
        spec.exclude_artists = _split_names(col2.text_input("Sin estos artistas", placeholder="separados por comas"))
        col1, col2 = st.columns(2)
        spec.min_plays = int(col1.number_input("Escuchas mínimas por canción", min_value=1, max_value=500, value=1))
        spec.min_seconds = int(col2.number_input("Segundos mínimos para contar", min_value=0, max_value=300,
                                                 value=spec.min_seconds, step=5,
                                                 help="Escuchas más cortas no cuentan (saltos)."))
        spec.avoid_skipped = st.checkbox("Evitar las canciones que sueles saltar",
                                         help="Fuera las que saltaste más de la mitad de las veces "
                                              "(dato del historial importado).")

    col1, col2 = st.columns([3, 1])
    name = col1.text_input("Nombre de la playlist", value=f"Mi selección - {label}")
    spec.limit = int(col2.number_input("Canciones", min_value=10, max_value=500, value=50, step=10))
    st.caption(spec.describe())

    tracks = _cached_build(ctx.conn, ctx.tz, ctx.user_id, str(ctx.tz), spec.to_json(), _data_version(ctx))
    if not tracks:
        st.warning("Ninguna canción cumple estos filtros. Prueba con un periodo más amplio o importa tu "
                   "historial en **Importar historial**.")
        return
    _playlist_actions(ctx, name, spec, tracks, key="custom")
    _tracks_table(tracks)


def page_rediscover(ctx: Ctx) -> None:
    st.title("Redescubrimientos")
    st.caption("Canciones que escuchaste mucho y llevas tiempo sin poner. Si vuelves a escucharlas, "
               "salen de la lista en la siguiente actualización.")
    spec = smart.Spec(kind="rediscover", range_type="all", min_plays=5,
                      min_seconds=get_min_play_ms() // 1000)
    col1, col2 = st.columns(2)
    epoch = col1.segmented_control("Época", ["Cualquier momento", "Un año concreto"],
                                   default="Cualquier momento") or "Cualquier momento"
    label = ""
    if epoch == "Un año concreto":
        year = col2.selectbox("Año", _years(ctx))
        spec.range_type, spec.start, spec.end = "fixed", f"{year}-01-01", f"{year}-12-31"
        label = f" {year}"
    spec.forgotten_months = st.select_slider(
        "Sin escuchar desde hace", options=[3, 6, 12, 18, 24, 36], value=6,
        format_func=lambda m: f"{m} meses" if m % 12 else (f"{m // 12} año" + ("s" if m > 12 else "")),
    )
    col1, col2, col3 = st.columns([2, 1, 1])
    name = col1.text_input("Nombre de la playlist", value=f"Redescubrimientos{label}")
    spec.min_plays = int(col2.number_input("Escuchas mínimas entonces", min_value=1, max_value=500, value=5))
    spec.limit = int(col3.number_input("Canciones", min_value=10, max_value=500, value=50, step=10))
    spec.avoid_skipped = st.checkbox("Evitar las que solías saltar")

    tracks = _cached_build(ctx.conn, ctx.tz, ctx.user_id, str(ctx.tz), spec.to_json(), _data_version(ctx))
    if not tracks:
        st.info("No hay canciones olvidadas con estos criterios. Prueba a bajar las escuchas mínimas, "
                "acortar el tiempo sin escucharlas o importar más historial.")
        return
    _playlist_actions(ctx, name, spec, tracks, key="rediscover")
    _tracks_table(tracks, smart.last_played(ctx.conn, ctx.user_id, [t.uri for t in tracks]), ctx.tz)


def page_stats(ctx: Ctx) -> None:
    st.title("Estadísticas")
    if history.coverage(ctx.conn, ctx.user_id).total == 0:
        st.info("Aún no hay escuchas. Importa tu historial en **Importar historial** para ver tus estadísticas.")
        return
    today = datetime.date.today()
    col1, col2 = st.columns([3, 1])
    choice = col1.segmented_control("Periodo", ["30 días", "Este año", "Un año", "Todo"],
                                    default="Este año") or "Este año"
    if choice == "30 días":  # días naturales: el rango no cambia a cada segundo (caché estable)
        start_ts, end_ts = range_for_dates(today - datetime.timedelta(days=29), today, ctx.tz)
    elif choice == "Este año":
        start_ts, end_ts = range_for_dates(datetime.date(today.year, 1, 1), today, ctx.tz)
    elif choice == "Un año":
        year = col2.selectbox("Año", _years(ctx))
        start_ts, end_ts = range_for_dates(datetime.date(year, 1, 1), datetime.date(year, 12, 31), ctx.tz)
    else:
        start_ts, end_ts = range_for_dates(datetime.date(MIN_YEAR, 1, 1), today, ctx.tz)

    result = _cached_stats(ctx.conn, ctx.tz, ctx.user_id, str(ctx.tz), start_ts, end_ts,
                           get_min_play_ms(), _data_version(ctx))
    if result.plays == 0:
        st.info("No hay escuchas en este periodo.")
        return

    def fmt(n) -> str:
        return f"{n:,.0f}".replace(",", ".")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Escuchas", fmt(result.plays))
    c2.metric("Horas" + (" (aprox.)" if result.hours_estimated else ""), fmt(result.hours),
              help="Del registro por API no se sabe cuánto duró cada escucha: se estima con la media."
              if result.hours_estimated else None)
    c3.metric("Canciones distintas", fmt(result.tracks))
    c4.metric("Artistas distintos", fmt(result.artists))

    with st.container(border=True):
        st.subheader("Escuchas por " + ("día" if result.period_unit == "day" else "mes"))
        st.altair_chart(charts.period_bars(result.by_period, result.period_unit), width="stretch")

    col1, col2 = st.columns(2)
    with col1, st.container(border=True):
        st.subheader("Artistas más escuchados")
        st.altair_chart(charts.ranked_bars(result.top_artists, "Artista"), width="stretch")
    with col2, st.container(border=True):
        st.subheader("Cuándo escuchas")
        st.caption("Día de la semana y hora. Más intenso = más escuchas.")
        st.altair_chart(charts.week_heatmap(result.heatmap), width="stretch")

    with st.container(border=True):
        st.subheader("Canciones más escuchadas")
        st.dataframe([{"#": i, "Canción": name, "Artista": artist, "Escuchas": n}
                      for i, (name, artist, n) in enumerate(result.top_tracks, 1)],
                     hide_index=True, width="stretch")

    with st.expander("Ver los datos de los gráficos", icon=":material/table:"):
        st.dataframe([{"Periodo": k, "Escuchas": n} for k, n in result.by_period], hide_index=True)
        st.dataframe([{"Artista": a, "Escuchas": n} for a, n in result.top_artists], hide_index=True)


def page_recipes(ctx: Ctx) -> None:
    st.title("Mis recetas")
    st.caption("Playlists que se recalculan solas cada día con sus filtros (junto con las automáticas). "
               "Se crean marcando «Mantenerla actualizada cada día» en Playlist personalizada o "
               "Redescubrimientos.")
    recipes = history.list_recipes(ctx.conn, ctx.user_id)
    if not recipes:
        st.info("Todavía no tienes recetas.")
        return
    if st.button("Actualizar todas ahora", icon=":material/autorenew:"):
        with st.spinner("Actualizando..."):
            try:
                outcomes = jobs.update_recipes(ctx.sp, ctx.conn, ctx.user_id, ctx.tz)
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_error(exc))
                outcomes = []
        for outcome in outcomes:
            _show_outcome(outcome)
    for i, (name, raw) in enumerate(recipes):
        with st.container(border=True):
            st.subheader(name)
            try:
                spec = smart.Spec.from_json(raw)
                st.caption(f"{spec.describe()} · {spec.limit} canciones")
            except (ValueError, TypeError) as exc:
                spec = None
                st.error(f"Receta no válida: {exc}")
            col1, col2, _ = st.columns([1, 1, 3])
            if spec is not None and col1.button("Actualizar", key=f"recipe_run_{i}", icon=":material/sync:"):
                try:
                    _show_outcome(jobs.update_recipe(ctx.sp, ctx.conn, ctx.user_id, name, spec, ctx.tz))
                except Exception as exc:  # noqa: BLE001
                    st.error(friendly_error(exc))
            if col2.button("Borrar receta", key=f"recipe_del_{i}", icon=":material/delete:",
                           help="Deja de actualizarse. La playlist se queda en tu Spotify."):
                history.delete_recipe(ctx.conn, ctx.user_id, name)
                st.rerun()


def page_spotify_top(ctx: Ctx) -> None:
    st.title("Top de Spotify")
    st.caption("Spotify solo ofrece tres periodos aproximados; no se pueden elegir fechas.")
    ranges = {
        "short_term": "Últimas ≈4 semanas",
        "medium_term": "Últimos ≈6 meses",
        "long_term": "Aprox. último año o más",
    }
    time_range = st.segmented_control("Periodo", list(ranges), format_func=ranges.get, default="short_term")
    time_range = time_range or "short_term"
    col1, col2 = st.columns([3, 1])
    name = col1.text_input("Nombre", value=f"Top Spotify - {ranges[time_range]}")
    limit = col2.number_input("Canciones", min_value=10, max_value=100, value=50, step=10)
    if st.button("Crear playlist del ranking", type="primary", icon=":material/playlist_add:",
                 disabled=not name.strip()):
        try:
            uris = get_clean_track_uris(get_top_tracks(ctx.sp, time_range, int(limit)))
            result = jobs.write_playlist(ctx.sp, ctx.conn, ctx.user_id, name.strip(), uris,
                                         f"Ranking de Spotify: {ranges[time_range]}.")
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
            return
        if result.playlist_id is None:
            st.warning("Spotify no devolvió canciones para ese periodo.")
        else:
            st.success(f"Playlist '{name}' lista con {result.track_count} canciones. "
                       f"[Abrir en Spotify]({result.url})")


def page_account(ctx: Ctx) -> None:
    st.title("Tu cuenta")
    st.write(f"Conectado como **{ctx.display_name}** (`{ctx.user_id}`).")
    with st.container(border=True):
        st.subheader("Cerrar sesión")
        st.caption("Sales de la web en este navegador. Tus escuchas se siguen registrando cada hora.")
        if st.button("Cerrar sesión", icon=":material/logout:"):
            _logout()
            st.rerun()
    with st.container(border=True):
        st.subheader("Olvidar mi cuenta")
        st.caption("Borra tu token de Spotify guardado: se deja de registrar lo que escuchas. "
                   "Tu historial y tus playlists no se tocan. Puedes volver a entrar cuando quieras.")
        sure = st.checkbox("Entiendo que dejará de registrarse lo que escucho")
        if st.button("Olvidar mi cuenta", disabled=not sure, icon=":material/person_remove:"):
            _logout(forget=True)
            st.rerun()


# ---------------------------------------------------------------- arranque

def _pages(ctx: Ctx, cloud: bool) -> dict:
    def page(fn, title, icon, path, **kw):
        return st.Page(lambda: fn(ctx), title=title, icon=icon, url_path=path, **kw)

    has_history = ctx.conn is not None
    music = [page(page_home, "Inicio", ":material/home:", "inicio", default=True)]
    create = []
    settings = []
    if has_history:
        music.append(page(page_stats, "Estadísticas", ":material/insights:", "estadisticas"))
        create += [
            page(page_custom, "Playlist personalizada", ":material/tune:", "personalizada"),
            page(page_rediscover, "Redescubrimientos", ":material/history:", "redescubrimientos"),
            page(page_recipes, "Mis recetas", ":material/auto_awesome:", "recetas"),
        ]
        settings.append(page(page_import, "Importar historial", ":material/upload_file:", "importar"))
    create.append(page(page_spotify_top, "Top de Spotify", ":material/trophy:", "top-spotify"))
    if cloud:
        settings.append(page(page_account, "Tu cuenta", ":material/account_circle:", "cuenta"))
    sections = {"Tu música": music, "Crear playlists": create}
    if settings:
        sections["Ajustes"] = settings
    return sections


def main() -> None:
    st.set_page_config(page_title="autospoti", page_icon="🎧", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    _apply_streamlit_secrets()
    cloud = use_db_tokens()

    try:
        tz = get_timezone()
        conn = _session_conn() if history_enabled() else None
    except ConfigError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # noqa: BLE001
        st.error(f"No se pudo abrir la base de datos: {type(exc).__name__}: {exc}")
        st.stop()

    try:
        if cloud:
            sp = _cloud_session(conn)
        else:
            _check_password()
            sp = _cached_file_client()
        user_info = st.session_state.get("user_info") or sp.current_user()
        st.session_state["user_info"] = user_info
    except AuthRequiredError:
        if cloud:  # token borrado o caducado sin remedio: volver a entrar
            _logout()
            st.rerun()
        raise
    except ConfigError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # noqa: BLE001 - se muestra la causa real, no un mensaje genérico
        st.error(f"No se pudo conectar con Spotify. {friendly_error(exc)}")
        st.stop()

    user_id = user_info["id"]
    display_name = user_info.get("display_name") or user_id
    if conn is not None and not cloud and not st.session_state.get("orphans_claimed"):
        # Historial migrado del esquema antiguo (sin usuario): pasa a ser del dueño.
        history.claim_orphan_plays(conn, user_id)
        st.session_state["orphans_claimed"] = True

    with st.sidebar:
        st.markdown('<div class="as-kicker">autospoti</div>'
                    f'<div class="as-user">{html.escape(display_name)}</div>',
                    unsafe_allow_html=True)
        if not cloud and not os.environ.get("APP_PASSWORD"):
            st.caption("Modo local sin contraseña.")

    ctx = Ctx(sp=sp, conn=conn, user_id=user_id, display_name=display_name, tz=tz)
    st.navigation(_pages(ctx, cloud)).run()


if __name__ == "__main__":
    main()
