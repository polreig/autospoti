"""Tareas de alto nivel compartidas por la app web (Streamlit) y por cron.py."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import spotipy

import history
from config import get_min_play_ms
from dateranges import range_last_days
from spotify_core import (
    PlaylistResult,
    create_or_update_playlist,
    get_clean_track_uris,
    get_recently_played,
    get_top_tracks,
)

# Nombres de las playlists automáticas (se identifican por nombre exacto).
NAME_RECENT = "Últimas reproducciones"
NAME_7D = "Más escuchado (7 días)"
NAME_4W = "Más escuchado (≈4 semanas)"
NAME_6M = "Más escuchado (≈6 meses)"
NAME_YEAR = "Más escuchado (último año)"

@dataclass
class JobOutcome:
    name: str
    result: "PlaylistResult | None"
    note: str = ""          # explicación cuando no se pudo actualizar

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.playlist_id is not None


def record_recent_plays(sp: spotipy.Spotify, conn: sqlite3.Connection) -> tuple[int, int]:
    """Guarda en la BD las últimas reproducciones (opción A). Devuelve (nuevas, duplicadas)."""
    rows = history.rows_from_recently_played(get_recently_played(sp))
    return history.add_plays(conn, rows, "api")


def _from_history(
    sp: spotipy.Spotify,
    conn: sqlite3.Connection,
    user_id: str,
    name: str,
    start_ts: int,
    end_ts: int,
    description: str,
    limit: int = 100,
) -> JobOutcome:
    tracks = history.top_tracks(conn, start_ts, end_ts, limit, min_ms=get_min_play_ms())
    if not tracks:
        return JobOutcome(name, None, "No hay reproducciones registradas en ese periodo.")
    result = create_or_update_playlist(sp, user_id, name, [t.uri for t in tracks], description)
    return JobOutcome(name, result)


def _from_spotify_top(
    sp: spotipy.Spotify, user_id: str, name: str, time_range: str, limit: int, description: str
) -> JobOutcome:
    uris = get_clean_track_uris(get_top_tracks(sp, time_range, limit))
    if not uris:
        return JobOutcome(name, None, "Spotify no devolvió canciones para este periodo.")
    return JobOutcome(name, create_or_update_playlist(sp, user_id, name, uris, description))


def _update_recent(sp: spotipy.Spotify, user_id: str) -> JobOutcome:
    recent = get_clean_track_uris(get_recently_played(sp))
    if not recent:
        return JobOutcome(NAME_RECENT, None, "Spotify no devolvió reproducciones recientes.")
    result = create_or_update_playlist(
        sp, user_id, NAME_RECENT, recent,
        "Mis últimas reproducciones (máx. 50, sin duplicados). No es un ranking.",
    )
    return JobOutcome(NAME_RECENT, result)


def _update_spotify_rankings(sp: spotipy.Spotify, user_id: str) -> list[JobOutcome]:
    """Rankings de Spotify (no existe un top exacto de 30/180 días en la API)."""
    return [
        _from_spotify_top(sp, user_id, NAME_4W, "short_term", 50,
                          "Mi top de canciones de las últimas ~4 semanas (Spotify)."),
        _from_spotify_top(sp, user_id, NAME_6M, "medium_term", 100,
                          "Mi top de canciones de los últimos ~6 meses (Spotify)."),
    ]


def _update_api_only(sp: spotipy.Spotify, user_id: str) -> list[JobOutcome]:
    return [_update_recent(sp, user_id), *_update_spotify_rankings(sp, user_id)]


def update_auto_playlists(
    sp: spotipy.Spotify,
    conn: "sqlite3.Connection | None",
    user_id: str,
    api_only: bool = False,
) -> list[JobOutcome]:
    """Registra las escuchas recientes y actualiza las playlists automáticas.

    Con ``api_only=True`` (GitHub Actions, Streamlit Cloud: sin disco persistente) no se usa
    la base de datos y solo se actualizan las playlists que salen de la API de Spotify
    (últimas reproducciones, ≈4 semanas y ≈6 meses). Las de 7 días y último año NO se tocan:
    dependen del registro propio y las pisaría con datos incompletos.
    """
    if api_only:
        return _update_api_only(sp, user_id)
    if conn is None:
        raise ValueError("Se necesita la base de datos del historial (o usa api_only=True).")
    record_recent_plays(sp, conn)
    outcomes: list[JobOutcome] = [_update_recent(sp, user_id)]

    # Más escuchado 7 días: se calcula con el registro propio.
    start, end = range_last_days(7)
    outcome = _from_history(
        sp, conn, user_id, NAME_7D, start, end,
        "Canciones que más he reproducido en los últimos 7 días (registro propio).",
    )
    if not outcome.ok:
        cov = history.coverage(conn)
        outcome.note += f" (reproducciones registradas en total: {cov.total})"
    outcomes.append(outcome)

    outcomes.extend(_update_spotify_rankings(sp, user_id))

    # Último año: exacto con el historial si cubre 365 días; si no, ranking de Spotify.
    # Exacto solo si el registro CUBRE el año (hay datos desde antes de hace 365 días)
    # y además hay reproducciones dentro de ese año. Si no, se usa el ranking de Spotify.
    year_start, year_end = range_last_days(365)
    cov = history.coverage(conn)
    covers_year = cov.first_ts is not None and cov.first_ts <= year_start
    if covers_year and history.count_plays(conn, year_start, year_end) > 0:
        outcomes.append(_from_history(
            sp, conn, user_id, NAME_YEAR, year_start, year_end,
            "Canciones que más he reproducido en el último año (registro propio + historial exportado).",
        ))
    else:
        outcomes.append(_from_spotify_top(
            sp, user_id, NAME_YEAR, "long_term", 100,
            "Mi top de canciones del último año (Spotify)."))
    return outcomes
