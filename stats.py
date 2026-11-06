"""Estadísticas de escucha para el panel (sin Streamlit): se calculan en Python sobre las
escuchas del periodo, porque meses / días / horas dependen de la zona horaria."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable

from history import PlayEvent

# El registro por API no informa de cuánto se escuchó: para estimar horas se usa la
# duración media de las escuchas conocidas (o esta, si no hay ninguna).
DEFAULT_PLAY_MS = 210_000


@dataclass
class Stats:
    plays: int = 0
    hours: float = 0.0              # aproximadas si hay escuchas sin duración
    hours_estimated: bool = False
    tracks: int = 0
    artists: int = 0
    first_ts: "int | None" = None
    last_ts: "int | None" = None
    by_period: list[tuple[str, int]] = field(default_factory=list)   # ("2024-03", n) o ("2024-03-05", n)
    period_unit: str = "month"                                       # "month" | "day"
    heatmap: list[tuple[int, int, int]] = field(default_factory=list)  # (día 0=lunes, hora, n)
    top_artists: list[tuple[str, int]] = field(default_factory=list)
    top_tracks: list[tuple[str, str, int]] = field(default_factory=list)  # (canción, artista, n)


def _months_between(first: date, last: date) -> list[str]:
    out, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def compute(events: Iterable[PlayEvent], tz: tzinfo, min_ms: int = 30_000,
            top_n: int = 15, day_granularity_max_days: int = 62) -> Stats:
    """Escuchas válidas = duración desconocida o >= ``min_ms`` (igual que las playlists)."""
    valid = [ev for ev in events if ev.ms_played is None or ev.ms_played >= min_ms]
    result = Stats()
    if not valid:
        return result

    known_ms = [ev.ms_played for ev in valid if ev.ms_played is not None]
    unknown = len(valid) - len(known_ms)
    avg_ms = (sum(known_ms) / len(known_ms)) if known_ms else DEFAULT_PLAY_MS
    result.plays = len(valid)
    result.hours = (sum(known_ms) + unknown * avg_ms) / 3_600_000
    result.hours_estimated = unknown > 0
    result.tracks = len({ev.track_uri for ev in valid})
    result.first_ts, result.last_ts = valid[0].ts, valid[-1].ts

    local = [datetime.fromtimestamp(ev.ts, tz) for ev in valid]
    first_day, last_day = local[0].date(), local[-1].date()
    if (last_day - first_day).days <= day_granularity_max_days:
        result.period_unit = "day"
        counts = Counter(dt.date().isoformat() for dt in local)
        days = [(first_day + timedelta(days=i)).isoformat() for i in range((last_day - first_day).days + 1)]
        result.by_period = [(d, counts.get(d, 0)) for d in days]
    else:
        counts = Counter(f"{dt.year:04d}-{dt.month:02d}" for dt in local)
        result.by_period = [(m, counts.get(m, 0)) for m in _months_between(first_day, last_day)]

    cells = Counter((dt.weekday(), dt.hour) for dt in local)
    result.heatmap = [(d, h, cells.get((d, h), 0)) for d in range(7) for h in range(24)]

    artists = Counter(ev.artist for ev in valid if ev.artist)
    result.artists = len(artists)
    result.top_artists = artists.most_common(top_n)

    track_counts = Counter(ev.track_uri for ev in valid)
    names = {ev.track_uri: (ev.track_name or "(sin título)", ev.artist or "") for ev in valid}
    result.top_tracks = [(*names[uri], n) for uri, n in track_counts.most_common(top_n)]
    return result
