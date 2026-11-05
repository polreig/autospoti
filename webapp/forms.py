"""Traduce los parámetros de los formularios de la web a recetas (smart.Spec).

Todo valor llega como texto desde la URL; aquí se valida y se acota. Un valor raro nunca
provoca un error 500: se usa el valor por defecto o se lanza ValueError con un mensaje
en español que la página muestra.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from typing import Mapping, Sequence

import blend
import smart

MIN_YEAR = 2008


class Params:
    """Acceso cómodo a query params (QueryParams de Starlette o dict de listas)."""

    def __init__(self, data):
        self._data = data

    def get(self, key: str, default: str = "") -> str:
        value = self._data.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        return str(value).strip() if value is not None else default

    def getlist(self, key: str) -> list[str]:
        if hasattr(self._data, "getlist"):
            return [v for v in self._data.getlist(key) if v != ""]
        value = self._data.get(key, [])
        return [str(v) for v in (value if isinstance(value, (list, tuple)) else [value]) if v != ""]

    def int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(self.get(key) or default)))
        except ValueError:
            return default

    def ints(self, key: str, lo: int, hi: int) -> list[int]:
        out = set()
        for raw in self.getlist(key):
            try:
                value = int(raw)
            except ValueError:
                continue
            if lo <= value <= hi:
                out.add(value)
        return sorted(out)


def _names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()][:20]


def _date(text: str, label: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"La fecha «{label}» no es válida.") from exc


def _apply_period(p: Params, spec: smart.Spec, today: date) -> str:
    """Rellena el periodo de la receta y devuelve una etiqueta para el nombre por defecto."""
    period = p.get("period", "dias")
    if period == "mes":
        year = p.int("year", today.year, MIN_YEAR, today.year)
        month = p.int("month", today.month, 1, 12)
        spec.range_type = "fixed"
        spec.start = f"{year}-{month:02d}-01"
        spec.end = f"{year}-{month:02d}-{monthrange(year, month)[1]:02d}"
        return f"{smart.MESES_ES[month - 1]} {year}"
    if period == "fechas":
        start = _date(p.get("start"), "desde")
        end = _date(p.get("end"), "hasta")
        if end < start:
            raise ValueError("La fecha final es anterior a la inicial.")
        spec.range_type, spec.start, spec.end = "fixed", start.isoformat(), end.isoformat()
        return f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"
    if period == "todo":
        spec.range_type = "all"
        return "de siempre"
    spec.range_type = "last_days"
    spec.days = p.int("days", 30, 1, 3650)
    return f"últimos {spec.days} días"


def _apply_filters(p: Params, spec: smart.Spec) -> None:
    spec.months = p.ints("m", 1, 12)
    spec.weekdays = p.ints("d", 0, 6)
    franjas = [f for f in p.getlist("f") if f in smart.FRANJAS]
    spec.hours = sorted({h for f in franjas for h in smart.FRANJAS[f]})
    spec.include_artists = _names(p.get("inc"))
    spec.exclude_artists = _names(p.get("exc"))
    spec.min_plays = p.int("min_plays", 1, 1, 500)
    spec.avoid_skipped = p.get("avoid") == "1"


def custom_spec(data, today: date, min_seconds: int) -> tuple[smart.Spec, str]:
    """Playlist personalizada. Devuelve (receta, nombre sugerido)."""
    p = Params(data)
    spec = smart.Spec(kind="top", min_seconds=p.int("min_seconds", min_seconds, 0, 300))
    label = _apply_period(p, spec, today)
    _apply_filters(p, spec)
    if spec.range_type == "fixed" and p.get("period") == "mes":
        spec.months = []  # el mes ya acota; un filtro de meses aquí solo confundiría
    spec.limit = p.int("limit", 50, 10, 500)
    spec.validate()
    return spec, f"Mi selección - {label}"


def rediscover_spec(data, min_seconds: int) -> tuple[smart.Spec, str]:
    p = Params(data)
    spec = smart.Spec(kind="rediscover", range_type="all", min_seconds=min_seconds)
    label = ""
    if p.get("epoch") == "anio":
        year = p.int("year", date.today().year - 1, MIN_YEAR, date.today().year)
        spec.range_type, spec.start, spec.end = "fixed", f"{year}-01-01", f"{year}-12-31"
        label = f" {year}"
    spec.forgotten_months = p.int("forgotten", 6, 1, 120)
    spec.min_plays = p.int("min_plays", 5, 1, 500)
    spec.limit = p.int("limit", 50, 10, 500)
    spec.avoid_skipped = p.get("avoid") == "1"
    spec.validate()
    return spec, f"Redescubrimientos{label}"


def blend_spec(data, partners: Sequence[str], names: Mapping[str, str],
               min_seconds: int) -> "tuple[smart.Spec, str] | None":
    """Mezcla de pareja. None si no tienes ninguna pareja enlazada."""
    if not partners:
        return None
    p = Params(data)
    partner = p.get("partner") if p.get("partner") in partners else partners[0]
    mode = p.get("mode") if p.get("mode") in blend.MODES else "comunes"
    spec = smart.Spec(kind="blend", partner=partner, blend_mode=mode, min_seconds=min_seconds)
    days = p.get("days", "90")
    if days == "todo":
        spec.range_type = "all"
    else:
        spec.range_type, spec.days = "last_days", p.int("days", 90, 1, 3650)
    spec.limit = p.int("limit", 50, 10, 500)
    spec.validate()
    return spec, f"Mezcla con {names.get(partner, partner)}"
