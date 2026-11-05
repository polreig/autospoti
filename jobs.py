"""Tareas de alto nivel compartidas por la app web (Streamlit) y por cron.py."""
from __future__ import annotations

from dataclasses import dataclass

from datetime import tzinfo

import spotipy

import history
import smart
from db import Database
from config import get_min_play_ms
from dateranges import range_last_days
from spotify_core import (
    PlaylistIndex,
    PlaylistResult,
    create_or_update_playlist,
    get_clean_track_uris,
    get_recently_played,
    get_top_tracks,
)

# Nombres de las playlists automáticas (al crearlas; después se reconocen por su ID guardado).
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


@dataclass
class _Ctx:
    """Lo que comparten todas las escrituras de una ejecución."""
    sp: spotipy.Spotify
    conn: "Database | None"
    user_id: str
    index: PlaylistIndex


def write_playlist(
    sp: spotipy.Spotify,
    conn: "Database | None",
    user_id: str,
    name: str,
    uris: list[str],
    description: str,
    index: "PlaylistIndex | None" = None,
) -> PlaylistResult:
    """Crea/actualiza una playlist recordando su ID en la BD (si hay BD).

    Así, si la renombras en Spotify, la app sigue actualizando la misma en vez de crear otra.
    """
    known_id = history.get_playlist_id(conn, user_id, name) if conn is not None else None
    result = create_or_update_playlist(
        sp, user_id, name, uris, description,
        index=index or PlaylistIndex(sp, user_id), playlist_id=known_id,
    )
    if conn is not None and result.playlist_id and result.playlist_id != known_id:
        history.save_playlist_id(conn, user_id, name, result.playlist_id)
    return result


def _write(ctx: _Ctx, name: str, uris: list[str], description: str) -> PlaylistResult:
    return write_playlist(ctx.sp, ctx.conn, ctx.user_id, name, uris, description, ctx.index)


def record_recent_plays(sp: spotipy.Spotify, conn: Database, user_id: str) -> tuple[int, int]:
    """Guarda en la BD las últimas reproducciones (opción A). Devuelve (nuevas, duplicadas)."""
    rows = history.rows_from_recently_played(get_recently_played(sp))
    return history.add_plays(conn, user_id, rows, "api")


def _from_history(
    ctx: _Ctx, name: str, start_ts: int, end_ts: int, description: str, limit: int = 100
) -> JobOutcome:
    tracks = history.top_tracks(ctx.conn, ctx.user_id, start_ts, end_ts, limit, min_ms=get_min_play_ms())
    if not tracks:
        return JobOutcome(name, None, "No hay reproducciones registradas en ese periodo.")
    return JobOutcome(name, _write(ctx, name, [t.uri for t in tracks], description))


def _from_spotify_top(ctx: _Ctx, name: str, time_range: str, limit: int, description: str) -> JobOutcome:
    uris = get_clean_track_uris(get_top_tracks(ctx.sp, time_range, limit))
    if not uris:
        return JobOutcome(name, None, "Spotify no devolvió canciones para este periodo.")
    return JobOutcome(name, _write(ctx, name, uris, description))


def _update_recent(ctx: _Ctx) -> JobOutcome:
    recent = get_clean_track_uris(get_recently_played(ctx.sp))
    if not recent:
        return JobOutcome(NAME_RECENT, None, "Spotify no devolvió reproducciones recientes.")
    result = _write(ctx, NAME_RECENT, recent,
                    "Mis últimas reproducciones (máx. 50, sin duplicados). No es un ranking.")
    return JobOutcome(NAME_RECENT, result)


def _update_spotify_rankings(ctx: _Ctx) -> list[JobOutcome]:
    """Rankings de Spotify (no existe un top exacto de 30/180 días en la API)."""
    return [
        _from_spotify_top(ctx, NAME_4W, "short_term", 50,
                          "Mi top de canciones de las últimas ~4 semanas (Spotify)."),
        _from_spotify_top(ctx, NAME_6M, "medium_term", 100,
                          "Mi top de canciones de los últimos ~6 meses (Spotify)."),
    ]


def update_recipe(
    sp: spotipy.Spotify,
    conn: Database,
    user_id: str,
    name: str,
    spec: "smart.Spec",
    tz: tzinfo,
    index: "PlaylistIndex | None" = None,
    daily: bool = True,
) -> JobOutcome:
    """Recalcula una receta y escribe su playlist. Sin canciones no toca la playlist."""
    tracks = smart.build_tracks(conn, user_id, spec, tz)
    if not tracks:
        return JobOutcome(name, None, "Ninguna canción cumple los filtros ahora mismo.")
    suffix = "autospoti, se actualiza cada día" if daily else "autospoti"
    description = f"{spec.describe()} ({suffix})."
    return JobOutcome(name, write_playlist(sp, conn, user_id, name, [t.uri for t in tracks],
                                           description, index))


def update_recipes(
    sp: spotipy.Spotify,
    conn: Database,
    user_id: str,
    tz: tzinfo,
    index: "PlaylistIndex | None" = None,
) -> list[JobOutcome]:
    """Regenera todas las recetas guardadas del usuario. Una receta rota no para al resto."""
    index = index or PlaylistIndex(sp, user_id)
    outcomes: list[JobOutcome] = []
    for name, raw in history.list_recipes(conn, user_id):
        try:
            outcomes.append(update_recipe(sp, conn, user_id, name, smart.Spec.from_json(raw), tz, index))
        except (ValueError, TypeError) as exc:
            outcomes.append(JobOutcome(name, None, f"Receta no válida: {exc}"))
    return outcomes


def update_auto_playlists(
    sp: spotipy.Spotify,
    conn: "Database | None",
    user_id: str,
    api_only: bool = False,
    index: "PlaylistIndex | None" = None,
) -> list[JobOutcome]:
    """Registra las escuchas recientes y actualiza las playlists automáticas.

    Con ``api_only=True`` (GitHub Actions, Streamlit Cloud: sin disco persistente) no se usa
    la base de datos y solo se actualizan las playlists que salen de la API de Spotify
    (últimas reproducciones, ≈4 semanas y ≈6 meses). Las de 7 días y último año NO se tocan:
    dependen del registro propio y las pisaría con datos incompletos.
    """
    index = index or PlaylistIndex(sp, user_id)  # se descarga una sola vez para todas
    if api_only:
        ctx = _Ctx(sp, None, user_id, index)
        return [_update_recent(ctx), *_update_spotify_rankings(ctx)]
    if conn is None:
        raise ValueError("Se necesita la base de datos del historial (o usa api_only=True).")
    ctx = _Ctx(sp, conn, user_id, index)
    record_recent_plays(sp, conn, user_id)
    outcomes: list[JobOutcome] = [_update_recent(ctx)]

    # Más escuchado 7 días: se calcula con el registro propio.
    start, end = range_last_days(7)
    outcome = _from_history(
        ctx, NAME_7D, start, end,
        "Canciones que más he reproducido en los últimos 7 días (registro propio).",
    )
    if not outcome.ok:
        cov = history.coverage(conn, user_id)
        outcome.note += f" (reproducciones registradas en total: {cov.total})"
    outcomes.append(outcome)

    outcomes.extend(_update_spotify_rankings(ctx))

    # Último año: exacto solo si el registro CUBRE el año (hay datos desde antes de hace
    # 365 días) y además hay reproducciones dentro de ese año. Si no, ranking de Spotify.
    year_start, year_end = range_last_days(365)
    cov = history.coverage(conn, user_id)
    covers_year = cov.first_ts is not None and cov.first_ts <= year_start
    if covers_year and history.count_plays(conn, user_id, year_start, year_end) > 0:
        outcomes.append(_from_history(
            ctx, NAME_YEAR, year_start, year_end,
            "Canciones que más he reproducido en el último año (registro propio + historial exportado).",
        ))
    else:
        outcomes.append(_from_spotify_top(
            ctx, NAME_YEAR, "long_term", 100,
            "Mi top de canciones del último año (Spotify)."))
    return outcomes
