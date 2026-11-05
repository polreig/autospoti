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

import history
import jobs
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


def page_custom(ctx: Ctx) -> None:
    st.title("Playlist de un periodo")
    st.caption("Lo que más escuchaste en las fechas que elijas (registro propio + historial importado). "
               f"Zona horaria: {ctx.tz}")
    today = datetime.date.today()
    mode = st.segmented_control("Periodo", ["Un mes", "Rango de fechas"], default="Un mes")

    if mode == "Rango de fechas":
        col1, col2 = st.columns(2)
        start = col1.date_input("Desde", value=today - datetime.timedelta(days=30),
                                min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        end = col2.date_input("Hasta (incluido)", value=today,
                              min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        if end < start:
            st.error("La fecha final es anterior a la inicial.")
            return
        start_ts, end_ts = range_for_dates(start, end, ctx.tz)
        label = f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"
    else:
        col1, col2 = st.columns(2)
        month = col1.selectbox("Mes", range(1, 13), index=today.month - 1,
                               format_func=lambda m: MESES_ES[m - 1])
        year = col2.number_input("Año", min_value=MIN_YEAR, max_value=today.year, value=today.year, step=1)
        start_ts, end_ts = range_for_month(int(year), int(month), ctx.tz)
        label = month_label(int(year), int(month))

    col1, col2, col3 = st.columns([2, 1, 1])
    name = col1.text_input("Nombre de la playlist", value=f"Mi selección - {label}")
    limit = col2.number_input("Canciones", min_value=10, max_value=500, value=50, step=10)
    min_seconds = col3.number_input("Segundos mínimos", min_value=0, max_value=300,
                                    value=get_min_play_ms() // 1000, step=5,
                                    help="Escuchas más cortas no cuentan (saltos).")

    tracks = history.top_tracks(ctx.conn, ctx.user_id, start_ts, end_ts, int(limit),
                                min_ms=int(min_seconds) * 1000)
    if not tracks:
        st.warning("No hay reproducciones en ese periodo. Importa tu historial en **Importar historial**.")
        return
    if st.button("Crear / actualizar playlist", type="primary", icon=":material/playlist_add:",
                 disabled=not name.strip()):
        try:
            result = jobs.write_playlist(
                ctx.sp, ctx.conn, ctx.user_id, name.strip(), [t.uri for t in tracks],
                f"Más escuchado: {label} (generado por autospoti).",
            )
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
        else:
            state = "creada" if result.created else "actualizada"
            st.success(f"Playlist '{name}' {state} con {result.track_count} canciones. "
                       f"[Abrir en Spotify]({result.url})")
    st.dataframe(
        [{"#": i, "Canción": t.name, "Artista": t.artist, "Escuchas": t.plays}
         for i, t in enumerate(tracks, 1)],
        hide_index=True, width="stretch",
    )


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

def _pages(ctx: Ctx, cloud: bool) -> list:
    def page(fn, title, icon, path, **kw):
        return st.Page(lambda: fn(ctx), title=title, icon=icon, url_path=path, **kw)

    pages = [page(page_home, "Inicio", ":material/home:", "inicio", default=True)]
    if ctx.conn is not None:
        pages += [
            page(page_custom, "Playlist de un periodo", ":material/date_range:", "periodo"),
            page(page_import, "Importar historial", ":material/upload_file:", "importar"),
        ]
    pages.append(page(page_spotify_top, "Top de Spotify", ":material/trophy:", "top-spotify"))
    if cloud:
        pages.append(page(page_account, "Tu cuenta", ":material/account_circle:", "cuenta"))
    return pages


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
