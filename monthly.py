"""Playlists mensuales: las 100 canciones más escuchadas de cada mes, con nombres tipo «September 25'».

Aquí solo se decide QUÉ meses faltan (sin escribir en Spotify); la escritura está en
``jobs.create_monthly_playlists``. Un mes se da por hecho si ya tienes una playlist con un
nombre que se reconoce como ese mes (también «Sept 25», «September 2025», «Septiembre 25'»...)
o si autospoti ya la creó alguna vez (aunque después la borraras: no se vuelve a crear).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo

import history
from db import Database
from dateranges import range_for_month

MONTHLY_LIMIT = 100        # canciones por playlist mensual
MONTHLY_MIN_TRACKS = 10    # con menos canciones distintas el mes no se crea (meses casi vacíos)

EN_MONTHS = ("January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December")
_ES_MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")

# Palabra (en minúsculas) -> número de mes. Nombres completos y abreviaturas, inglés y español.
_MONTH_WORDS: dict[str, int] = {}
for _i, (_en, _es) in enumerate(zip(EN_MONTHS, _ES_MONTHS), 1):
    for _word in (_en.lower(), _en.lower()[:3], _es, _es[:3]):
        _MONTH_WORDS[_word] = _i
_MONTH_WORDS.update({"sept": 9, "setiembre": 9, "set": 9})

_APOSTROPHES = re.compile(r"[’‘´`′]")
_NAME_RE = re.compile(r"^([a-zñ]+)\.?,?\s*'?\s*(\d{2}|\d{4})\s*'?$")

# Estados de un mes en el plan
EXISTS = "tienes"          # ya hay una playlist tuya con ese mes
MANAGED = "gestionada"     # autospoti la creó antes (renombrada o borrada): no se repite
FEW = "pocas"              # menos de MONTHLY_MIN_TRACKS canciones distintas
TO_CREATE = "crear"


def monthly_name(year: int, month: int) -> str:
    """«September 25'»: mes en inglés, dos últimas cifras del año y apóstrofo."""
    return f"{EN_MONTHS[month - 1]} {year % 100:02d}'"


def parse_monthly_name(name: str) -> "tuple[int, int] | None":
    """(año, mes) si el nombre es el de una playlist mensual; si no, None."""
    text = _APOSTROPHES.sub("'", " ".join((name or "").casefold().split()))
    match = _NAME_RE.match(text)
    if not match or match.group(1) not in _MONTH_WORDS:
        return None
    year = int(match.group(2))
    if year < 100:
        year += 2000
    return year, _MONTH_WORDS[match.group(1)]


@dataclass
class MonthPlan:
    year: int
    month: int
    name: str
    status: str
    tracks: list = field(default_factory=list)   # history.TopTrack (solo si se ha calculado)

    @property
    def key(self) -> tuple[int, int]:
        return self.year, self.month


def previous_month(now: datetime, tz: tzinfo) -> tuple[int, int]:
    local = now.astimezone(tz)
    return (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)


def _months_between(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    year, month = first
    out = []
    while (year, month) <= last:
        out.append((year, month))
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return out


def plan(
    conn: Database,
    user_id: str,
    tz: tzinfo,
    playlist_names: list[str],
    now: "datetime | None" = None,
    min_ms: int = 0,
) -> list[MonthPlan]:
    """Todos los meses COMPLETOS de tu historial, del más antiguo al más reciente, con su estado.

    ``playlist_names``: los nombres de tus playlists en Spotify (``PlaylistIndex.names()``).
    El mes en curso nunca entra: se crea cuando termina.
    """
    cov = history.coverage(conn, user_id)
    if cov.first_ts is None:
        return []
    now = now or datetime.now(timezone.utc)
    first_local = datetime.fromtimestamp(cov.first_ts, tz)
    months = _months_between((first_local.year, first_local.month), previous_month(now, tz))
    existing = {parsed for parsed in map(parse_monthly_name, playlist_names) if parsed}
    managed = history.managed_names(conn, user_id)
    plans = []
    for year, month in months:
        name = monthly_name(year, month)
        if (year, month) in existing:
            plans.append(MonthPlan(year, month, name, EXISTS))
        elif name in managed:
            plans.append(MonthPlan(year, month, name, MANAGED))
        else:
            start, end = range_for_month(year, month, tz)
            tracks = history.top_tracks(conn, user_id, start, end, MONTHLY_LIMIT, min_ms=min_ms)
            status = TO_CREATE if len(tracks) >= MONTHLY_MIN_TRACKS else FEW
            plans.append(MonthPlan(year, month, name, status, tracks))
    return plans


def description(year: int, month: int) -> str:
    return (f"Mis {MONTHLY_LIMIT} canciones más escuchadas de {_ES_MONTHS[month - 1]} de {year} "
            "(autospoti).")
