"""Registro propio de reproducciones (SQLite) y consultas por rango de fechas.

Dos fuentes alimentan la misma tabla ``plays``:
  * ``api``    -> ``recently_played`` de Spotify (lo registra cron.py cada pocas horas).
  * ``export`` -> ficheros ``Streaming_History_Audio_*.json`` (ver history_import.py).

Cada reproducción pertenece a un usuario de Spotify (``user_id``), para que en el
futuro varias cuentas puedan compartir la misma base de datos sin mezclarse.

Todos los instantes se guardan como segundos Unix (UTC).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

from config import get_db_path

# Dos filas de fuentes distintas con la misma canción a menos de esta distancia
# (en segundos) se consideran la misma reproducción (evita contar doble al
# solapar el registro por API con el historial exportado).
NEAR_DUPLICATE_SECONDS = 120

# Versión del esquema (PRAGMA user_version). 0/1 = esquema antiguo sin user_id.
SCHEMA_VERSION = 2

# Usuario provisional de las filas migradas desde el esquema antiguo (sin user_id).
ORPHAN_USER = ""

PLAYS_TABLE = """
CREATE TABLE IF NOT EXISTS plays (
    user_id     TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    track_uri   TEXT    NOT NULL,
    track_name  TEXT,
    artist      TEXT,
    album       TEXT,
    ms_played   INTEGER,
    source      TEXT    NOT NULL,
    skipped     INTEGER,            -- 1/0; NULL si se desconoce (p. ej. registro por API)
    reason_end  TEXT,               -- trackdone, fwdbtn, endplay... (solo export)
    shuffle     INTEGER,            -- 1/0; NULL si se desconoce
    platform    TEXT,
    PRIMARY KEY (user_id, ts, track_uri)
);
"""

SCHEMA = PLAYS_TABLE + """
CREATE INDEX IF NOT EXISTS idx_plays_user_uri_ts ON plays (user_id, track_uri, ts);
CREATE INDEX IF NOT EXISTS idx_plays_user_ts ON plays (user_id, ts);

-- Playlists que gestiona la app: se recuerda su ID para no depender solo del nombre
-- (si la renombras en Spotify se sigue actualizando la misma).
CREATE TABLE IF NOT EXISTS managed_playlists (
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""

_INCOMING_TABLE = """
CREATE TEMP TABLE IF NOT EXISTS incoming (
    ts INTEGER, track_uri TEXT, track_name TEXT, artist TEXT, album TEXT, ms_played INTEGER,
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


def _needs_v2_migration(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plays)")}
    return bool(cols) and "user_id" not in cols


def _backup_file(conn: sqlite3.Connection, target: str) -> None:
    """Copia de seguridad antes de migrar (solo bases de datos en disco)."""
    if target == ":memory:" or not Path(target).exists():
        return
    backup = Path(target).with_suffix(".v1-backup.db")
    if not backup.exists():
        conn.commit()
        with sqlite3.connect(str(backup)) as dest:
            conn.backup(dest)
        dest.close()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Esquema antiguo (sin user_id) -> v2. Las filas quedan como huérfanas (ORPHAN_USER)
    hasta que ``claim_orphan_plays`` las asigna al dueño."""
    conn.executescript(
        "BEGIN;\n"
        "ALTER TABLE plays RENAME TO plays_v1;\n"
        + PLAYS_TABLE +
        "INSERT INTO plays (user_id, ts, track_uri, track_name, artist, album, ms_played, source)\n"
        "    SELECT '', ts, track_uri, track_name, artist, album, ms_played, source FROM plays_v1;\n"
        "DROP TABLE plays_v1;\n"
        "COMMIT;"
    )


def connect(path: "str | Path | None" = None, check_same_thread: bool = True) -> sqlite3.Connection:
    target = str(path) if path is not None else str(get_db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=check_same_thread)
    if target != ":memory:":
        # WAL: lecturas concurrentes (app web + cron) sin bloquearse mutuamente.
        conn.execute("PRAGMA journal_mode=WAL")
    if _needs_v2_migration(conn):
        _backup_file(conn, target)
        _migrate_to_v2(conn)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def claim_orphan_plays(conn: sqlite3.Connection, user_id: str) -> int:
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
    cur = conn.execute("UPDATE OR IGNORE plays SET user_id = ? WHERE user_id = ?", (user_id, ORPHAN_USER))
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
    conn: sqlite3.Connection, user_id: str, rows: Iterable[PlayRow | tuple], source: str
) -> tuple[int, int]:
    """Inserta reproducciones de ``user_id``. Devuelve (insertadas, duplicadas).

    Se hace en bloque (tabla temporal + un único INSERT ... SELECT) en vez de una consulta
    por fila: importar un historial de cientos de miles de escuchas tarda segundos.
    """
    if not user_id:
        raise ValueError("user_id vacío.")
    normalized = [PlayRow(*row) for row in rows]
    if not normalized:
        return 0, 0
    conn.execute(_INCOMING_TABLE)
    conn.execute("DELETE FROM temp.incoming")
    conn.executemany(
        "INSERT INTO temp.incoming VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r.ts, r.track_uri, r.track_name, r.artist, r.album, r.ms_played,
          _as_int(r.skipped), r.reason_end, _as_int(r.shuffle), r.platform) for r in normalized],
    )
    # Duplicado = misma clave exacta (INSERT OR IGNORE) o la misma canción registrada por
    # OTRA fuente a menos de NEAR_DUPLICATE_SECONDS (solapamiento API / export).
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO plays (user_id, ts, track_uri, track_name, artist, album, ms_played,
                                     source, skipped, reason_end, shuffle, platform)
        SELECT ?, i.ts, i.track_uri, i.track_name, i.artist, i.album, i.ms_played,
               ?, i.skipped, i.reason_end, i.shuffle, i.platform
        FROM temp.incoming AS i
        WHERE NOT EXISTS (
            SELECT 1 FROM plays AS p
            WHERE p.user_id = ? AND p.track_uri = i.track_uri
              AND p.ts BETWEEN i.ts - ? AND i.ts + ? AND p.source != ?
        )
        """,
        (user_id, source, user_id, NEAR_DUPLICATE_SECONDS, NEAR_DUPLICATE_SECONDS, source),
    )
    inserted = cur.rowcount
    conn.execute("DELETE FROM temp.incoming")
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
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
        rows.append(PlayRow(
            ts=parse_ts(played_at),
            track_uri=uri,
            track_name=track.get("name"),
            artist=artists or None,
            album=(track.get("album") or {}).get("name"),
            ms_played=None,  # la API no informa de cuánto se escuchó
        ))
    return rows


def coverage(conn: sqlite3.Connection, user_id: str) -> Coverage:
    total, first_ts, last_ts = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM plays WHERE user_id = ?", (user_id,)
    ).fetchone()
    return Coverage(total=total, first_ts=first_ts, last_ts=last_ts)


def count_plays(conn: sqlite3.Connection, user_id: str, start_ts: int, end_ts: int) -> int:
    """Número de reproducciones de ``user_id`` registradas en [start_ts, end_ts)."""
    return conn.execute(
        "SELECT COUNT(*) FROM plays WHERE user_id = ? AND ts >= ? AND ts < ?",
        (user_id, start_ts, end_ts),
    ).fetchone()[0]


def top_tracks(
    conn: sqlite3.Connection,
    user_id: str,
    start_ts: int,
    end_ts: int,
    limit: int,
    min_ms: int = 0,
) -> list[TopTrack]:
    """Canciones más reproducidas en [start_ts, end_ts). Desempata por la más reciente."""
    cur = conn.execute(
        """
        SELECT track_uri, MAX(track_name), MAX(artist), COUNT(*) AS plays
        FROM plays
        WHERE user_id = ? AND ts >= ? AND ts < ? AND (ms_played IS NULL OR ms_played >= ?)
        GROUP BY track_uri
        ORDER BY plays DESC, MAX(ts) DESC
        LIMIT ?
        """,
        (user_id, start_ts, end_ts, min_ms, limit),
    )
    return [TopTrack(uri=r[0], name=r[1] or "(sin título)", artist=r[2] or "", plays=r[3]) for r in cur]


def get_playlist_id(conn: sqlite3.Connection, user_id: str, name: str) -> "str | None":
    row = conn.execute(
        "SELECT playlist_id FROM managed_playlists WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    return row[0] if row else None


def save_playlist_id(conn: sqlite3.Connection, user_id: str, name: str, playlist_id: str) -> None:
    conn.execute(
        "INSERT INTO managed_playlists (user_id, name, playlist_id) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, name) DO UPDATE SET playlist_id = excluded.playlist_id",
        (user_id, name, playlist_id),
    )
    conn.commit()
