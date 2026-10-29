"""App web (Streamlit): playlists automáticas, historial propio y rangos personalizados.

Ejecutar:  streamlit run autoplaylist.py
"""
from __future__ import annotations

import datetime
import hmac
import os

import streamlit as st

import history
import jobs
from config import ConfigError, get_min_play_ms, get_timezone, history_enabled
from dateranges import MESES_ES, format_ts, month_label, range_for_dates, range_for_month
from history_import import import_export_bytes
from spotify_core import (
    AuthRequiredError,
    create_or_update_playlist,
    friendly_error,
    get_clean_track_uris,
    get_spotify_client,
    get_top_tracks,
)

MIN_YEAR = 2008  # Spotify no existía antes


def _apply_streamlit_secrets() -> None:
    """Si existe .streamlit/secrets.toml (Streamlit Cloud), lo expone como variables de entorno."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key, value)
    except Exception:  # noqa: BLE001 - sin secrets.toml no hay nada que cargar
        pass


def _check_password() -> None:
    """Protege la app con APP_PASSWORD (el cliente de Spotify actúa sobre TU cuenta)."""
    password = os.environ.get("APP_PASSWORD", "")
    if not password:
        st.sidebar.warning("APP_PASSWORD no está definido: cualquiera con acceso a la URL usa tu cuenta.")
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
def _cached_client():
    return get_spotify_client()


@st.cache_resource
def _cached_conn():
    """Conexión SQLite compartida entre sesiones de Streamlit (hilos distintos)."""
    import sqlite3

    from config import get_db_path
    from history import SCHEMA

    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.executescript(SCHEMA)
    return conn


def _show_outcome(outcome: jobs.JobOutcome) -> None:
    if outcome.ok:
        state = "creada" if outcome.result.created else "actualizada"
        st.success(f"**{outcome.name}**: {state} con {outcome.result.track_count} canciones. "
                   f"[Abrir]({outcome.result.url})")
    else:
        st.warning(f"**{outcome.name}**: no se actualizó. {outcome.note}")


def tab_auto(sp, conn, user_id: str, tz) -> None:
    """``conn`` es None si el historial está desactivado (HISTORY_ENABLED=false)."""
    st.header("Listas automáticas")
    api_only = conn is None
    if api_only:
        st.info("Modo sin historial (sin disco persistente): se actualizan solo las playlists que salen "
                "de la API de Spotify. 'Más escuchado (7 días)' y 'último año' se actualizan desde tu PC.")
        st.write(
            "- **Últimas reproducciones**: tus últimas 50 escuchas (no es un ranking).\n"
            "- **≈4 semanas y ≈6 meses**: rankings de Spotify (la API no ofrece 30/180 días exactos)."
        )
    else:
        cov = history.coverage(conn)
        st.caption(
            f"Registro propio: {cov.total} reproducciones, de {format_ts(cov.first_ts, tz)} "
            f"a {format_ts(cov.last_ts, tz)}."
        )
        st.write(
            "- **Últimas reproducciones**: tus últimas 50 escuchas (no es un ranking).\n"
            "- **Más escuchado (7 días)**: calculado con tu registro propio.\n"
            "- **≈4 semanas y ≈6 meses**: rankings de Spotify (la API no ofrece 30/180 días exactos).\n"
            "- **Último año**: exacto si tu historial cubre 365 días; si no, el ranking de Spotify."
        )
    label = "Actualizar las 3 playlists" if api_only else "Actualizar las 5 playlists"
    if st.button(label, type="primary"):
        try:
            with st.spinner("Procesando canciones..."):
                outcomes = jobs.update_auto_playlists(sp, conn, user_id, api_only=api_only)
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
            return
        for outcome in outcomes:
            _show_outcome(outcome)


def tab_import(conn, tz) -> None:
    st.header("Importar historial de Spotify")
    st.write(
        "Sube los archivos **Streaming_History_Audio_*.json** de tu *Extended streaming history* "
        "(Spotify > Privacidad > Descargar tus datos). Puedes subir varios a la vez; "
        "las repeticiones se ignoran, así que no pasa nada si importas el mismo archivo dos veces."
    )
    files = st.file_uploader("Archivos JSON", type="json", accept_multiple_files=True)
    if files and st.button("Importar", type="primary"):
        total = None
        for uploaded in files:
            try:
                stats = import_export_bytes(conn, uploaded.getvalue())
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
    cov = history.coverage(conn)
    st.info(f"Registro actual: {cov.total} reproducciones ({format_ts(cov.first_ts, tz)} → "
            f"{format_ts(cov.last_ts, tz)}).")


def tab_custom(sp, conn, user_id: str, tz) -> None:
    st.header("Playlist de un periodo personalizado")
    st.caption("Usa tu registro propio e historial importado. Zona horaria: " + str(tz))
    today = datetime.date.today()
    mode = st.radio("Periodo", ["Un mes", "Rango de fechas"], horizontal=True)

    if mode == "Un mes":
        col1, col2 = st.columns(2)
        month = col1.selectbox("Mes", range(1, 13), index=today.month - 1,
                               format_func=lambda m: MESES_ES[m - 1])
        year = col2.number_input("Año", min_value=MIN_YEAR, max_value=today.year, value=today.year, step=1)
        start_ts, end_ts = range_for_month(int(year), int(month), tz)
        label = month_label(int(year), int(month))
    else:
        col1, col2 = st.columns(2)
        start = col1.date_input("Desde", value=today - datetime.timedelta(days=30),
                                min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        end = col2.date_input("Hasta (incluido)", value=today,
                              min_value=datetime.date(MIN_YEAR, 1, 1), max_value=today)
        if end < start:
            st.error("La fecha final es anterior a la inicial.")
            return
        start_ts, end_ts = range_for_dates(start, end, tz)
        label = f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"

    name = st.text_input("Nombre de la playlist", value=f"Mi selección - {label}")
    limit = st.slider("Número de canciones", min_value=10, max_value=100, value=50)
    min_seconds = st.number_input("Segundos mínimos para contar una escucha", min_value=0, max_value=300,
                                  value=get_min_play_ms() // 1000, step=5)

    tracks = history.top_tracks(conn, start_ts, end_ts, limit, min_ms=int(min_seconds) * 1000)
    if not tracks:
        st.warning("No hay reproducciones en ese periodo. Importa tu historial en la pestaña anterior.")
        return
    st.dataframe(
        [{"#": i, "Canción": t.name, "Artista": t.artist, "Reproducciones": t.plays}
         for i, t in enumerate(tracks, 1)],
        hide_index=True, use_container_width=True,
    )
    if not name.strip():
        st.error("Pon un nombre a la playlist.")
        return
    if st.button("Crear / actualizar playlist"):
        try:
            result = create_or_update_playlist(
                sp, user_id, name.strip(), [t.uri for t in tracks],
                f"Más escuchado: {label} (generado por autospoti).",
            )
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
            return
        state = "creada" if result.created else "actualizada"
        st.success(f"Playlist '{name}' {state} con {result.track_count} canciones. [Abrir]({result.url})")



def tab_spotify_top(sp, user_id: str) -> None:
    st.header("Rankings directos de Spotify")
    st.caption("Spotify solo ofrece tres periodos aproximados; no se pueden elegir fechas.")
    ranges = {
        "short_term": "Últimas ≈4 semanas",
        "medium_term": "Últimos ≈6 meses",
        "long_term": "Aprox. último año o más (todos los tiempos)",
    }
    time_range = st.selectbox("Periodo", list(ranges), format_func=ranges.get)
    name = st.text_input("Nombre", value=f"Top Spotify - {ranges[time_range]}")
    limit = st.slider("Canciones", min_value=10, max_value=100, value=50, key="spotify_top_limit")
    if st.button("Crear playlist del ranking"):
        if not name.strip():
            st.error("Pon un nombre a la playlist.")
            return
        try:
            uris = get_clean_track_uris(get_top_tracks(sp, time_range, limit))
            result = create_or_update_playlist(sp, user_id, name.strip(), uris,
                                               f"Ranking de Spotify: {ranges[time_range]}.")
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error(exc))
            return
        if result.playlist_id is None:
            st.warning("Spotify no devolvió canciones para ese periodo.")
        else:
            st.success(f"Playlist '{name}' lista con {result.track_count} canciones. [Abrir]({result.url})")


def main() -> None:
    st.set_page_config(page_title="Mi Spotify Stats", page_icon="🎵", layout="centered")
    st.title("🎧 Mi Creador de Playlists Pro")
    _apply_streamlit_secrets()
    _check_password()

    try:
        tz = get_timezone()
        sp = _cached_client()
        user_info = sp.current_user()
        user_id = user_info["id"]
        st.sidebar.success(f"Conectado como: **{user_info.get('display_name') or user_id}**")
    except (ConfigError, AuthRequiredError) as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # noqa: BLE001 - se muestra la causa real, no un mensaje genérico
        st.error(f"No se pudo conectar con Spotify. {friendly_error(exc)}")
        st.stop()

    if not history_enabled():
        # Sin historial (p. ej. Streamlit Cloud): solo automáticas por API y rankings de Spotify.
        tabs = st.tabs(["⚡ Automáticas", "🏆 Top de Spotify"])
        with tabs[0]:
            tab_auto(sp, None, user_id, tz)
        with tabs[1]:
            tab_spotify_top(sp, user_id)
        return

    conn = _cached_conn()
    tabs = st.tabs(["⚡ Automáticas", "📥 Importar historial", "🗓️ Periodo personalizado", "🏆 Top de Spotify"])
    with tabs[0]:
        tab_auto(sp, conn, user_id, tz)
    with tabs[1]:
        tab_import(conn, tz)
    with tabs[2]:
        tab_custom(sp, conn, user_id, tz)
    with tabs[3]:
        tab_spotify_top(sp, user_id)


if __name__ == "__main__":
    main()

