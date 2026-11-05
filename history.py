"""Registro propio de reproducciones y consultas por rango de fechas.

Dos fuentes alimentan la misma tabla ``plays``:
  * ``api``    -> ``recently_played`` de Spotify (lo registra cron.py cada hora).
  * ``export`` -> ficheros ``Streaming_History_Audio_*.json`` (ver history_import.py).

Cada reproducción pertenece a un usuario de Spotify (``user_id``). La BD puede ser
SQLite local o Postgres en la nube (ver db.py); las consultas son SQL portable.

Todos los instantes se guardan como segundos Unix (UTC).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

import db as dbmod
from db import SCHEMA_VERSION, Database  # noqa: F401 - reexportados

# Dos filas de fuentes distintas con la misma canción a menos de esta distancia
# (en segundos) se consideran la misma reproducción (evita contar doble al
# solapar el registro por API con el historial exportado).
NEAR_DUPLICATE_SECONDS = 120

# Usuario provisional de las filas migradas desde el esquema antiguo (sin user_id).
ORPHAN_USER = ""

_INCOMING_COLUMNS = ("ts", "track_uri", "track_name", "artist", "album", "ms_played",
                     "skipped", "reason_end", "shuffle", "platform")
_INCOMING_TABLE = """
CREATE TEMP TABLE IF NOT EXISTS incoming (
    ts BIGINT, track_uri TEXT, track_name TEXT, artist TEXT, album TEXT, ms_played INTEGER,
    skipped INTEGER, reason_end TEXT, shuffle INTEGER, platform TEXT
)
"""


class PlayRow(NamedTuple):
    ts: int
    track_uri: str
    track_name: "str | None"
    artist: "str | None"
    album: "str | None"
    ms_played: "int | None"
    skipped: "bool | None" = None
    reason_end: "str | None" = None
    shuffle: "bool | None" = None
    platform: "str | None" = None


@dataclass
class TopTrack:
    uri: str
    name: str
    artist: str
    plays: int


@dataclass
class Coverage:
    total: int
    first_ts: "int | None"
    last_ts: "int | None"


def connect(path: "str | Path | None" = None, check_same_thread: bool = True) -> Database:
    """Abre la BD (ver ``db.connect``): ruta/URL explícita, DATABASE_URL o data/history.db."""
    return dbmod.connect(path, check_same_thread=check_same_thread)


def claim_orphan_plays(conn: Database, user_id: str) -> int:
    """Asigna a ``user_id`` las filas migradas sin usuario. Devuelve cuántas se asignaron.

    Solo lo hace si la BD aún es de un único usuario (no hay filas de otra cuenta): así
    el historial antiguo nunca acaba en la cuenta equivocada cuando haya varios usuarios.
    """
    if not user_id:
        raise ValueError("user_id vacío.")
    other = conn.execute(
        "SELECT 1 FROM plays WHERE user_id NOT IN (?, ?) LIMIT 1", (ORPHAN_USER, user_id)
    ).fetchone()
    if other:
        return 0
    cur = conn.execute(
        """
        UPDATE plays SET user_id = ?
        WHERE user_id = ? AND NOT EXISTS (
            SELECT 1 FROM plays AS mine
            WHERE mine.user_id = ? AND mine.ts = plays.ts AND mine.track_uri = plays.track_uri
        )
        """,
        (user_id, ORPHAN_USER, user_id),
    )
    conn.commit()
    return cur.rowcount


def parse_ts(value: str) -> int:
    """Convierte '2022-03-05T18:42:11Z' (con o sin milisegundos) a segundos Unix UTC."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _as_int(flag: "bool | None") -> "int | None":
    return None if flag is None else int(bool(flag))


def add_plays(
    conn: Database, user_id: str, rows: Iterable[PlayRow | tuple], source: str
) -> tuple[int, int]:
    """Inserta reproducciones de ``user_id``. Devuelve (insertadas, duplicadas).

    Se hace en bloque (tabla temporal + un único INSERT ... SELECT) en vez de una consulta
    por fila: importar un historial de cientos de miles de escuchas tarda segundos, también
    contra una BD en la nube (en Postgres la carga usa COPY).
    """
    if not user_id:
        raise ValueError("user_id vacío.")
    normalized = [PlayRow(*row) for row in rows]
    if not normalized:
        return 0, 0
    conn.execute(_INCOMING_TABLE)
    conn.execute("DELETE FROM incoming")
    conn.copy_rows("incoming", _INCOMING_COLUMNS, [
        (r.ts, r.track_uri, r.track_name, r.artist, r.album, r.ms_played,
         _as_int(r.skipped), r.reason_end, _as_int(r.shuffle), r.platform) for r in normalized
    ])
    # Duplicado = misma clave exacta (ON CONFLICT) o la misma canción registrada por
    # OTRA fuente a menos de NEAR_DUPLICATE_SECONDS (solapamiento API / export).
    cur = conn.execute(
        """
        INSERT INTO plays (user_id, ts, track_uri, track_name, artist, album, ms_played,
                           source, skipped, reason_end, shuffle, platform)
        SELECT CAST(? AS TEXT), i.ts, i.track_uri, i.track_name, i.artist, i.album, i.ms_played,
               CAST(? AS TEXT), i.skipped, i.reason_end, i.shuffle, i.platform
        FROM incoming AS i
        WHERE NOT EXISTS (
            SELECT 1 FROM plays AS p
            WHERE p.user_id = ? AND p.track_uri = i.track_uri
              AND p.ts BETWEEN i.ts - ? AND i.ts + ? AND p.source != ?
        )
        ON CONFLICT DO NOTHING
        """,
        (user_id, source, user_id, NEAR_DUPLICATE_SECONDS, NEAR_DUPLICATE_SECONDS, source),
    )
    inserted = cur.rowcount
    conn.execute("DELETE FROM incoming")
    conn.commit()
    return inserted, len(normalized) - inserted


def rows_from_recently_played(items: list[dict]) -> list[PlayRow]:
    """Convierte la respuesta de ``current_user_recently_played`` en filas de la BD."""
    rows: list[PlayRow] = []
    for item in items:
        track = item.get("track") or {}
        uri = track.get("uri")
        played_at = item.get("played_at")
        # Se ignoran episodios de podcast y archivos locales (sin URI de canción).
        if not uri or not str(uri).startswith("spotify:track:") or not played_at:
            continue
        # Solo el artista principal: así coincide con el export (un único artista) y las
        # estadísticas por artista no separan "X" de "X, Y".
        names = [a.get("name") for a in track.get("artists", []) if a.get("name")]
        rows.append(PlayRow(
            ts=parse_ts(played_at),
            track_uri=uri,
            track_name=track.get("name"),
            artist=names[0] if names else None,
            album=(track.get("album") or {}).get("name"),
            ms_played=None,  # la API no informa de cuánto se escuchó
        ))
    return rows


def coverage(conn: Database, user_id: str) -> Coverage:
    total, first_ts, last_ts = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM plays WHERE user_id = ?", (user_id,)
    ).fetchone()
    return Coverage(total=total, first_ts=first_ts, last_ts=last_ts)


def count_plays(conn: Database, user_id: str, start_ts: int, end_ts: int) -> int:
    """Número de reproducciones de ``user_id`` registradas en [start_ts, end_ts)."""
    return conn.execute(
        "SELECT COUNT(*) FROM plays WHERE user_id = ? AND ts >= ? AND ts < ?",
        (user_id, start_ts, end_ts),
    ).fetchone()[0]


def top_tracks(
    conn: Database,
    user_id: str,
    start_ts: int,
    end_ts: int,
    limit: int,
    min_ms: int = 0,
) -> list[TopTrack]:
    """Canciones más reproducidas en [start_ts, end_ts). Desempata por la más reciente."""
    cur = conn.execute(
        """
        SELECT track_uri, MAX(track_name), MAX(artist), COUNT(*)
        FROM plays
        WHERE user_id = ? AND ts >= ? AND ts < ? AND (ms_played IS NULL OR ms_played >= ?)
        GROUP BY track_uri
        ORDER BY COUNT(*) DESC, MAX(ts) DESC
        LIMIT ?
        """,
        (user_id, start_ts, end_ts, min_ms, limit),
    )
    return [TopTrack(uri=r[0], name=r[1] or "(sin título)", artist=r[2] or "", plays=r[3])
            for r in cur.fetchall()]


class PlayEvent(NamedTuple):
    """Una escucha tal cual, para estadísticas y filtros que se calculan en Python
    (hora/día de la semana dependen de la zona horaria)."""
    ts: int
    track_uri: str
    track_name: "str | None"
    artist: "str | None"
    ms_played: "int | None"
    skipped: "int | None"


def fetch_plays(conn: Database, user_id: str, start_ts: int, end_ts: int) -> list[PlayEvent]:
    """Todas las escuchas de ``user_id`` en [start_ts, end_ts), en orden cronológico."""
    cur = conn.execute(
        "SELECT ts, track_uri, track_name, artist, ms_played, skipped FROM plays "
        "WHERE user_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (user_id, start_ts, end_ts),
    )
    return [PlayEvent(*row) for row in cur.fetchall()]


def uris_played_since(conn: Database, user_id: str, since_ts: int) -> set[str]:
    """Canciones con alguna escucha (aunque sea corta) desde ``since_ts``."""
    cur = conn.execute(
        "SELECT DISTINCT track_uri FROM plays WHERE user_id = ? AND ts >= ?", (user_id, since_ts)
    )
    return {row[0] for row in cur.fetchall()}


def save_recipe(conn: Database, user_id: str, name: str, spec_json: str) -> None:
    conn.execute(
        "INSERT INTO recipes (user_id, name, spec, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, name) DO UPDATE SET spec = excluded.spec, updated_at = excluded.updated_at",
        (user_id, name, spec_json, int(datetime.now(timezone.utc).timestamp())),
    )
    conn.commit()


def list_recipes(conn: Database, user_id: str) -> list[tuple[str, str]]:
    """[(nombre, spec_json)] ordenadas por nombre."""
    cur = conn.execute("SELECT name, spec FROM recipes WHERE user_id = ? ORDER BY name", (user_id,))
    return [(row[0], row[1]) for row in cur.fetchall()]


def delete_recipe(conn: Database, user_id: str, name: str) -> None:
    conn.execute("DELETE FROM recipes WHERE user_id = ? AND name = ?", (user_id, name))
    conn.commit()


def get_playlist_id(conn: Database, user_id: str, name: str) -> "str | None":
    row = conn.execute(
        "SELECT playlist_id FROM managed_playlists WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    return row[0] if row else None


def save_playlist_id(conn: Database, user_id: str, name: str, playlist_id: str) -> None:
    conn.execute(
        "INSERT INTO managed_playlists (user_id, name, playlist_id) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, name) DO UPDATE SET playlist_id = excluded.playlist_id",
        (user_id, name, playlist_id),
    )
    conn.commit()
