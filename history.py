"""Registro propio de reproducciones (SQLite) y consultas por rango de fechas.

Dos fuentes alimentan la misma tabla ``plays``:
  * ``api``    -> ``recently_played`` de Spotify (lo registra cron.py cada pocas horas).
  * ``export`` -> ficheros ``Streaming_History_Audio_*.json`` (ver history_import.py).

Todos los instantes se guardan como segundos Unix (UTC).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from config import get_db_path

# Dos filas de fuentes distintas con la misma canción a menos de esta distancia
# (en segundos) se consideran la misma reproducción (evita contar doble al
# solapar el registro por API con el historial exportado).
NEAR_DUPLICATE_SECONDS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    ts          INTEGER NOT NULL,
    track_uri   TEXT    NOT NULL,
    track_name  TEXT,
    artist      TEXT,
    album       TEXT,
    ms_played   INTEGER,
    source      TEXT    NOT NULL,
    PRIMARY KEY (ts, track_uri)
);
CREATE INDEX IF NOT EXISTS idx_plays_uri_ts ON plays (track_uri, ts);
"""

# (ts, track_uri, track_name, artist, album, ms_played)
PlayRow = tuple[int, str, "str | None", "str | None", "str | None", "int | None"]


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


def connect(path: "str | Path | None" = None) -> sqlite3.Connection:
    target = str(path) if path is not None else str(get_db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.executescript(SCHEMA)
    return conn


def parse_ts(value: str) -> int:
    """Convierte '2022-03-05T18:42:11Z' (con o sin milisegundos) a segundos Unix UTC."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def add_plays(conn: sqlite3.Connection, rows: Iterable[PlayRow], source: str) -> tuple[int, int]:
    """Inserta reproducciones. Devuelve (insertadas, duplicadas)."""
    inserted = 0
    duplicates = 0
    for ts, uri, name, artist, album, ms_played in rows:
        near = conn.execute(
            "SELECT 1 FROM plays WHERE track_uri = ? AND ts BETWEEN ? AND ? AND source != ? LIMIT 1",
            (uri, ts - NEAR_DUPLICATE_SECONDS, ts + NEAR_DUPLICATE_SECONDS, source),
        ).fetchone()
        if near:
            duplicates += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO plays (ts, track_uri, track_name, artist, album, ms_played, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, uri, name, artist, album, ms_played, source),
        )
        if cur.rowcount == 1:
            inserted += 1
        else:
            duplicates += 1
    conn.commit()
    return inserted, duplicates


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
        rows.append((
            parse_ts(played_at),
            uri,
            track.get("name"),
            artists or None,
            (track.get("album") or {}).get("name"),
            None,  # la API no informa de cuánto se escuchó
        ))
    return rows


def coverage(conn: sqlite3.Connection) -> Coverage:
    total, first_ts, last_ts = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM plays").fetchone()
    return Coverage(total=total, first_ts=first_ts, last_ts=last_ts)


def count_plays(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> int:
    """Número de reproducciones registradas en [start_ts, end_ts)."""
    return conn.execute("SELECT COUNT(*) FROM plays WHERE ts >= ? AND ts < ?", (start_ts, end_ts)).fetchone()[0]


def top_tracks(
    conn: sqlite3.Connection,
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
        WHERE ts >= ? AND ts < ? AND (ms_played IS NULL OR ms_played >= ?)
        GROUP BY track_uri
        ORDER BY plays DESC, MAX(ts) DESC
        LIMIT ?
        """,
        (start_ts, end_ts, min_ms, limit),
    )
    return [TopTrack(uri=r[0], name=r[1] or "(sin título)", artist=r[2] or "", plays=r[3]) for r in cur]
