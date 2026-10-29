"""Cálculo de rangos de fechas (en segundos Unix UTC) para filtrar el historial."""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone, tzinfo

MESES_ES = (
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)


def range_last_days(days: int, now: "datetime | None" = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    return int((now - timedelta(days=days)).timestamp()), int(now.timestamp()) + 1


def range_for_dates(start: date, end: date, tz: tzinfo) -> tuple[int, int]:
    """Del día ``start`` (00:00) al final del día ``end`` (inclusive), en la zona ``tz``."""
    if end < start:
        raise ValueError("La fecha final es anterior a la inicial.")
    start_dt = datetime.combine(start, time.min, tzinfo=tz)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def range_for_month(year: int, month: int, tz: tzinfo) -> tuple[int, int]:
    last_day = calendar.monthrange(year, month)[1]
    return range_for_dates(date(year, month, 1), date(year, month, last_day), tz)


def month_label(year: int, month: int) -> str:
    return f"{MESES_ES[month - 1]} {year}"


def format_ts(ts: "int | None", tz: tzinfo) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz).strftime("%d/%m/%Y %H:%M")
