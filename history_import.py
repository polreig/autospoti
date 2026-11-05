"""Importación del historial exportado de Spotify (Extended streaming history).

Spotify entrega ficheros ``Streaming_History_Audio_*.json``: una lista de
objetos con ``ts`` (UTC), ``ms_played``, ``spotify_track_uri``,
``master_metadata_track_name``, ``master_metadata_album_artist_name``...
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Sequence

from history import PlayRow, add_plays, parse_ts


@dataclass
class ImportStats:
    files: int = 0
    entries: int = 0
    inserted: int = 0
    duplicates: int = 0
    no_uri: int = 0          # podcasts, archivos locales, entradas sin URI
    not_track: int = 0       # URIs que no son de canción (episodios, etc.)
    legacy_format: int = 0   # formato antiguo (StreamingHistory0.json) sin URIs
    invalid: int = 0         # entradas mal formadas

    def merge(self, other: "ImportStats") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


def rows_from_export(entries: Sequence[object]) -> tuple[list[PlayRow], ImportStats]:
    """Transforma las entradas del export en filas para la BD (sin escribirlas)."""
    stats = ImportStats(entries=len(entries))
    rows: list[PlayRow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            stats.invalid += 1
            continue
        uri = entry.get("spotify_track_uri")
        if not uri:
            if "trackName" in entry and "master_metadata_track_name" not in entry:
                stats.legacy_format += 1
            else:
                stats.no_uri += 1
            continue
        if not str(uri).startswith("spotify:track:"):
            stats.not_track += 1
            continue
        try:
            ts = parse_ts(entry["ts"])
        except (KeyError, ValueError, TypeError):
            stats.invalid += 1
            continue
        try:
            ms_played = int(entry["ms_played"]) if entry.get("ms_played") is not None else None
        except (TypeError, ValueError):
            ms_played = None
        rows.append(PlayRow(
            ts=ts,
            track_uri=str(uri),
            track_name=entry.get("master_metadata_track_name"),
            artist=entry.get("master_metadata_album_artist_name"),
            album=entry.get("master_metadata_album_album_name"),
            ms_played=ms_played,
            skipped=_optional_bool(entry.get("skipped")),
            reason_end=entry.get("reason_end") or None,
            shuffle=_optional_bool(entry.get("shuffle")),
            platform=entry.get("platform") or None,
        ))
    return rows, stats


def _optional_bool(value: object) -> "bool | None":
    """El export usa true/false/null (a veces 0/1)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def import_export_bytes(conn: sqlite3.Connection, user_id: str, data: bytes) -> ImportStats:
    """Importa el contenido de un único JSON del historial exportado."""
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON no válido: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("El JSON debe contener una lista de reproducciones.")
    rows, stats = rows_from_export(parsed)
    stats.files = 1
    stats.inserted, stats.duplicates = add_plays(conn, user_id, rows, "export")
    return stats


def find_export_files(paths: Sequence["str | Path"]) -> list[Path]:
    """Expande carpetas a sus ficheros Streaming_History_Audio_*.json (recursivo)."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("Streaming_History_Audio_*.json")))
        elif path.is_file():
            found.append(path)
        else:
            raise FileNotFoundError(f"No existe: {path}")
    return found


def import_export_files(
    conn: sqlite3.Connection, user_id: str, paths: Sequence["str | Path"]
) -> ImportStats:
    total = ImportStats()
    for file_path in find_export_files(paths):
        total.merge(import_export_bytes(conn, user_id, file_path.read_bytes()))
    return total
