"""Tareas de alto nivel compartidas por la app web (Streamlit) y por cron.py."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

import spotipy

import history
import monthly
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
# NO cambiarlos: el ID guardado va por nombre y se crearía una playlist nueva.
NAME_RECENT = "Últimas reproducciones"
NAME_7D = "Más escuchado (7 días)"
NAME_4W = "Más escuchado (≈4 semanas)"
NAME_6M = "Más escuchado (≈6 meses)"
NAME_YEAR = "Más escuchado (último año)"
NAME_ALWAYS = "Always"

# Canciones de cada automática. Últimas reproducciones: la API da como mucho 50.
LIMIT_7D = 50
LIMIT_4W = 100
LIMIT_6M = 100
LIMIT_YEAR = 100
LIMIT_ALWAYS = 250

# Días de cada periodo cuando se calcula con el registro propio.
DAYS_4W = 28
DAYS_6M = 182
DAYS_YEAR = 365


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
    """Rankings de Spotify (modo sin BD: no existe un top exacto de 4 semanas/6 meses en la API)."""
    return [
        _from_spotify_top(ctx, NAME_4W, "short_term", LIMIT_4W,
                          "Mi top de canciones de las últimas ~4 semanas (Spotify)."),
        _from_spotify_top(ctx, NAME_6M, "medium_term", LIMIT_6M,
                          "Mi top de canciones de los últimos ~6 meses (Spotify)."),
    ]


def _ranked(ctx: _Ctx, name: str, days: int, limit: int, label: str, time_range: str) -> JobOutcome:
    """Top de los últimos ``days`` días con el registro propio, exacto, si el registro CUBRE el
    periodo (hay escuchas desde antes de su inicio) y hay escuchas dentro. Si no, el ranking
    aproximado de Spotify (``time_range``)."""
    start, end = range_last_days(days)
    cov = history.coverage(ctx.conn, ctx.user_id)
    covers = cov.first_ts is not None and cov.first_ts <= start
    if covers and history.count_plays(ctx.conn, ctx.user_id, start, end) > 0:
        return _from_history(ctx, name, start, end,
                             f"Mis {limit} canciones más escuchadas {label} (registro propio, "
                             "se actualiza cada día).", limit)
    return _from_spotify_top(ctx, name, time_range, limit,
                             f"Mi top de canciones {label} (ranking de Spotify).")


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

    Con BD (6 playlists): últimas reproducciones (50), 7 días (50), 4 semanas (100),
    6 meses (100), último año (100) y Always (250, de siempre). Las de 4 semanas, 6 meses y
    año salen del registro propio si cubre el periodo; si no, del ranking de Spotify.

    Con ``api_only=True`` (sin base de datos) solo se actualizan las que salen de la API de
    Spotify (últimas reproducciones, ≈4 semanas y ≈6 meses). Las de 7 días, último año y
    Always NO se tocan: dependen del registro propio y las pisaría con datos incompletos.
    """
    index = index or PlaylistIndex(sp, user_id)  # se descarga una sola vez para todas
    if api_only:
        ctx = _Ctx(sp, None, user_id, index)
        return [_update_recent(ctx), *_update_spotify_rankings(ctx)]
    if conn is None:
        raise ValueError("Se necesita la base de datos del historial (o usa api_only=True).")
    ctx = _Ctx(sp, conn, user_id, index)
    record_recent_plays(sp, conn, user_id)  # antes de calcular: incluye lo último escuchado
    outcomes: list[JobOutcome] = [_update_recent(ctx)]

    # 7 días: siempre con el registro propio (Spotify no tiene un ranking semanal).
    start, end = range_last_days(7)
    outcome = _from_history(
        ctx, NAME_7D, start, end,
        f"Mis {LIMIT_7D} canciones más escuchadas en los últimos 7 días (registro propio, "
        "se actualiza cada día).", LIMIT_7D,
    )
    if not outcome.ok:
        cov = history.coverage(conn, user_id)
        outcome.note += (f" (reproducciones registradas en total: {cov.total}; revisa que el "
                         "registro de cada hora esté funcionando)")
    outcomes.append(outcome)

    outcomes.append(_ranked(ctx, NAME_4W, DAYS_4W, LIMIT_4W, "de las últimas 4 semanas", "short_term"))
    outcomes.append(_ranked(ctx, NAME_6M, DAYS_6M, LIMIT_6M, "de los últimos 6 meses", "medium_term"))
    outcomes.append(_ranked(ctx, NAME_YEAR, DAYS_YEAR, LIMIT_YEAR, "del último año", "long_term"))

    # Always: lo más escuchado de siempre (todo el registro + historial importado).
    cov = history.coverage(conn, user_id)
    if cov.first_ts is None:
        outcomes.append(JobOutcome(NAME_ALWAYS, None, "Aún no hay escuchas registradas."))
    else:
        outcomes.append(_from_history(
            ctx, NAME_ALWAYS, cov.first_ts, cov.last_ts + 1,
            f"Mis {LIMIT_ALWAYS} canciones más escuchadas de siempre (registro propio + historial "
            "importado, se actualiza cada día).", LIMIT_ALWAYS,
        ))
    return outcomes


# ---------------------------------------------------------------- playlists mensuales

def monthly_plan(sp: spotipy.Spotify, conn: Database, user_id: str, tz: tzinfo,
                 index: "PlaylistIndex | None" = None, now=None) -> list["monthly.MonthPlan"]:
    """Vista previa: qué meses tienen ya playlist y cuáles se crearían (no escribe nada)."""
    if history.coverage(conn, user_id).first_ts is None:
        return []  # sin escuchas no hay meses (y no hace falta descargar tus playlists)
    index = index or PlaylistIndex(sp, user_id)
    return monthly.plan(conn, user_id, tz, index.names(), now=now, min_ms=get_min_play_ms())


def create_monthly_playlists(
    sp: spotipy.Spotify,
    conn: Database,
    user_id: str,
    tz: tzinfo,
    index: "PlaylistIndex | None" = None,
    *,
    only_previous_month: bool = False,
    max_count: "int | None" = None,
    now=None,
) -> list[JobOutcome]:
    """Crea las playlists mensuales que faltan, de la más antigua a la más reciente.

    ``only_previous_month``: solo la del mes pasado (lo que hace la tarea diaria: el día 1
    aparece la del mes que acaba de terminar). Nunca toca una playlist mensual que ya exista.
    ``max_count``: cuántas como mucho en esta llamada (la web va por tandas).
    """
    index = index or PlaylistIndex(sp, user_id)
    now = now or datetime.now(timezone.utc)
    plans = [p for p in monthly_plan(sp, conn, user_id, tz, index, now) if p.status == monthly.TO_CREATE]
    if only_previous_month:
        last = monthly.previous_month(now, tz)
        plans = [p for p in plans if p.key == last]
    if max_count is not None:
        plans = plans[:max_count]
    outcomes = []
    for p in plans:
        result = write_playlist(sp, conn, user_id, p.name, [t.uri for t in p.tracks],
                                monthly.description(p.year, p.month), index)
        outcomes.append(JobOutcome(p.name, result))
    return outcomes
