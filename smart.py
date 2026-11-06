"""Playlists personalizadas con filtros ("recetas") y redescubrimientos. Sin Streamlit.

Una receta (``Spec``) describe QUÉ canciones elegir, no cuáles: se guarda en JSON y se
vuelve a calcular cada día (cron), así "últimos 90 días, domingos por la noche" se
mantiene al día sola.

Los filtros por hora / día de la semana / mes dependen de la zona horaria, así que se
aplican en Python sobre las escuchas del periodo (``history.fetch_plays``).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable

import blend
import history
from dateranges import MESES_ES, range_for_dates, range_last_days
from db import Database
from history import PlayEvent, TopTrack

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
DIAS_CORTOS = ("L", "M", "X", "J", "V", "S", "D")

# Franjas horarias que ofrece la interfaz (hora de inicio incluida, fin excluido).
FRANJAS = {
    "madrugada": range(0, 6),
    "mañana": range(6, 12),
    "tarde": range(12, 18),
    "noche": range(18, 24),
}

SKIP_RATE_LIMIT = 0.5  # "evitar las que sueles saltar": más de la mitad de las veces


@dataclass
class Spec:
    kind: str = "top"              # "top" | "rediscover"
    range_type: str = "last_days"  # "last_days" | "fixed" | "all"
    days: int = 90
    start: "str | None" = None     # fecha ISO (range_type == "fixed")
    end: "str | None" = None       # fecha ISO, incluida
    months: list[int] = field(default_factory=list)    # 1-12; vacío = todos
    weekdays: list[int] = field(default_factory=list)  # 0 = lunes; vacío = todos
    hours: list[int] = field(default_factory=list)     # 0-23; vacío = todas
    include_artists: list[str] = field(default_factory=list)
    exclude_artists: list[str] = field(default_factory=list)
    avoid_skipped: bool = False
    min_plays: int = 1
    min_seconds: int = 30
    limit: int = 50
    forgotten_months: int = 6      # rediscover: sin escuchar desde hace N meses
    partner: "str | None" = None   # blend: la otra cuenta (debe estar enlazada)
    blend_mode: str = "comunes"    # blend: "comunes" | "mitad"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Spec":
        """Ignora claves desconocidas (recetas guardadas por versiones futuras/antiguas)."""
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def validate(self) -> None:
        if self.kind not in ("top", "rediscover", "blend"):
            raise ValueError(f"Tipo de receta desconocido: {self.kind}")
        if self.kind == "blend":
            if not self.partner:
                raise ValueError("Falta la otra persona de la mezcla.")
            if self.blend_mode not in blend.MODES:
                raise ValueError(f"Modo de mezcla desconocido: {self.blend_mode}")
        if self.range_type not in ("last_days", "fixed", "all"):
            raise ValueError(f"Tipo de periodo desconocido: {self.range_type}")
        if self.range_type == "fixed":
            if not self.start or not self.end:
                raise ValueError("Falta la fecha de inicio o de fin.")
            if date.fromisoformat(self.end) < date.fromisoformat(self.start):
                raise ValueError("La fecha final es anterior a la inicial.")
        if self.range_type == "last_days" and self.days < 1:
            raise ValueError("El número de días debe ser al menos 1.")
        if not 1 <= self.limit <= 10_000:
            raise ValueError("El número de canciones debe estar entre 1 y 10.000.")

    def describe(self) -> str:
        """Descripción corta en español (para la playlist y la lista de recetas)."""
        parts: list[str] = []
        if self.kind == "rediscover":
            parts.append(f"Redescubrimientos: no escuchadas en {self.forgotten_months} meses")
        if self.kind == "blend":
            parts.append(f"Mezcla con {self.partner} ({blend.MODES.get(self.blend_mode, self.blend_mode)})")
        if self.range_type == "last_days":
            parts.append(f"últimos {self.days} días")
        elif self.range_type == "fixed":
            parts.append(f"{_fmt_date(self.start)} - {_fmt_date(self.end)}")
        elif self.kind != "rediscover":
            parts.append("todo el historial")
        if self.months:
            parts.append(", ".join(MESES_ES[m - 1].lower() for m in sorted(self.months)))
        if self.weekdays:
            parts.append(", ".join(DIAS[d] for d in sorted(self.weekdays)))
        if self.hours:
            parts.append(_describe_hours(self.hours))
        if self.include_artists:
            parts.append("solo " + ", ".join(self.include_artists))
        if self.exclude_artists:
            parts.append("sin " + ", ".join(self.exclude_artists))
        if self.avoid_skipped:
            parts.append("sin las que sueles saltar")
        if self.min_plays > 1:
            parts.append(f"mín. {self.min_plays} escuchas")
        text = " · ".join(parts)
        return text[:1].upper() + text[1:]


def _fmt_date(iso: "str | None") -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else "?"


def _describe_hours(hours: Iterable[int]) -> str:
    wanted = set(hours)
    names = [name for name, span in FRANJAS.items() if set(span) <= wanted]
    covered = set().union(*(FRANJAS[n] for n in names)) if names else set()
    if covered == wanted:
        return "por la " + ", ".join(names) if len(names) == 1 else ", ".join(names)
    return "horas " + ", ".join(f"{h}h" for h in sorted(wanted))


def resolve_range(spec: Spec, tz: tzinfo, now: "datetime | None" = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    if spec.range_type == "last_days":
        return range_last_days(spec.days, now)
    if spec.range_type == "fixed":
        return range_for_dates(date.fromisoformat(spec.start), date.fromisoformat(spec.end), tz)
    return 0, int(now.timestamp()) + 1


def _matches_artist(artist: str, needles: list[str]) -> bool:
    lowered = artist.lower()
    return any(n.lower() in lowered for n in needles if n.strip())


def aggregate(events: Iterable[PlayEvent], spec: Spec, tz: tzinfo,
              exclude_uris: "set[str] | None" = None) -> list[TopTrack]:
    """Aplica los filtros de la receta y devuelve el ranking (más escuchadas primero)."""
    min_ms = max(0, spec.min_seconds) * 1000
    months, weekdays, hours = set(spec.months), set(spec.weekdays), set(spec.hours)
    needs_time = bool(months or weekdays or hours)
    exclude_uris = exclude_uris or set()
    stats: dict[str, list] = {}  # uri -> [plays, last_ts, name, artist, skips, known_skip]
    for ev in events:
        if ev.track_uri in exclude_uris:
            continue
        artist = ev.artist or ""
        if spec.include_artists and not _matches_artist(artist, spec.include_artists):
            continue
        if spec.exclude_artists and _matches_artist(artist, spec.exclude_artists):
            continue
        if needs_time:
            local = datetime.fromtimestamp(ev.ts, tz)
            if (months and local.month not in months) or (weekdays and local.weekday() not in weekdays) \
                    or (hours and local.hour not in hours):
                continue
        entry = stats.setdefault(ev.track_uri, [0, 0, ev.track_name, ev.artist, 0, 0])
        entry[1] = max(entry[1], ev.ts)
        entry[2] = entry[2] or ev.track_name
        entry[3] = entry[3] or ev.artist
        if ev.skipped is not None:
            entry[5] += 1
            entry[4] += 1 if ev.skipped else 0
        if ev.ms_played is not None and ev.ms_played < min_ms:
            continue  # escucha demasiado corta: no cuenta como reproducción
        entry[0] += 1

    ranked = []
    for uri, (plays, last_ts, name, artist, skips, known) in stats.items():
        if plays < max(1, spec.min_plays):
            continue
        if spec.avoid_skipped and known and skips / known > SKIP_RATE_LIMIT:
            continue
        ranked.append((plays, last_ts, TopTrack(uri=uri, name=name or "(sin título)",
                                                artist=artist or "", plays=plays)))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [track for _, _, track in ranked[:spec.limit]]


def build_tracks(conn: Database, user_id: str, spec: Spec, tz: tzinfo,
                 now: "datetime | None" = None) -> list[TopTrack]:
    """Canciones de la receta. En "rediscover", solo las que llevas ``forgotten_months``
    sin escuchar: el periodo de búsqueda termina donde empieza el olvido."""
    spec.validate()
    now = now or datetime.now(timezone.utc)
    start_ts, end_ts = resolve_range(spec, tz, now)
    if spec.kind == "blend":
        # Sin enlace aceptado no se lee NADA del historial de la otra persona.
        if not blend.is_linked(conn, user_id, spec.partner):
            return []
        wide = replace(spec, limit=10_000) if spec.blend_mode == "comunes" else spec
        mine = aggregate(history.fetch_plays(conn, user_id, start_ts, end_ts), wide, tz)
        theirs = aggregate(history.fetch_plays(conn, spec.partner, start_ts, end_ts), wide, tz)
        return blend.combine(mine, theirs, spec.blend_mode, spec.limit)
    exclude: set[str] = set()
    if spec.kind == "rediscover":
        cutoff = int((now - timedelta(days=30 * spec.forgotten_months)).timestamp())
        end_ts = min(end_ts, cutoff)
        if end_ts <= start_ts:
            return []
        exclude = history.uris_played_since(conn, user_id, cutoff)
    events = history.fetch_plays(conn, user_id, start_ts, end_ts)
    return aggregate(events, spec, tz, exclude_uris=exclude)


def last_played(conn: Database, user_id: str, uris: list[str]) -> dict[str, int]:
    """Última escucha de cada canción (para mostrar "última vez" en los redescubrimientos)."""
    if not uris:
        return {}
    marks = ", ".join("?" for _ in uris)
    cur = conn.execute(
        f"SELECT track_uri, MAX(ts) FROM plays WHERE user_id = ? AND track_uri IN ({marks}) "
        "GROUP BY track_uri",
        (user_id, *uris),
    )
    return {row[0]: row[1] for row in cur.fetchall()}
