"""Gráficos del panel de estadísticas (Altair). Todos son de una sola serie: un único color
de acento, sin leyenda; la magnitud del mapa de calor va en la opacidad de ese color, así
se lee igual con tema oscuro o claro. Cada gráfico lleva tooltip y una tabla equivalente
en la interfaz (accesibilidad)."""
from __future__ import annotations

from datetime import date

import altair as alt

from dateranges import MESES_ES
from smart import DIAS, DIAS_CORTOS

ACCENT = "#A78BFA"


def _month_label(key: str) -> str:
    year, month = key.split("-")[:2]
    return f"{MESES_ES[int(month) - 1]} {year}"


def period_bars(by_period: list[tuple[str, int]], unit: str) -> alt.Chart:
    """Escuchas por mes (o por día en periodos cortos)."""
    rows = []
    for key, plays in by_period:
        if unit == "day":
            day = date.fromisoformat(key)
            rows.append({"fecha": key, "etiqueta": day.strftime("%d/%m/%Y"), "escuchas": plays})
        else:
            rows.append({"fecha": f"{key}-01", "etiqueta": _month_label(key), "escuchas": plays})
    time_unit = "yearmonthdate" if unit == "day" else "yearmonth"
    axis_format = "%d/%m" if unit == "day" else "%m/%Y"
    return (
        alt.Chart(alt.Data(values=rows))
        .mark_bar(color=ACCENT, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=alt.X(f"{time_unit}(fecha):T", title=None,
                    axis=alt.Axis(format=axis_format, labelAngle=0, grid=False)),
            y=alt.Y("escuchas:Q", title="Escuchas"),
            tooltip=[alt.Tooltip("etiqueta:N", title="Periodo"),
                     alt.Tooltip("escuchas:Q", title="Escuchas", format=",")],
        )
        .properties(height=260)
    )


def ranked_bars(items: list[tuple[str, int]], label: str) -> alt.Chart:
    """Ranking horizontal (más escuchado arriba)."""
    rows = [{"nombre": name, "escuchas": plays} for name, plays in items]
    return (
        alt.Chart(alt.Data(values=rows))
        .mark_bar(color=ACCENT, cornerRadiusEnd=4, height={"band": 0.75})
        .encode(
            y=alt.Y("nombre:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220, grid=False)),
            x=alt.X("escuchas:Q", title="Escuchas"),
            tooltip=[alt.Tooltip("nombre:N", title=label),
                     alt.Tooltip("escuchas:Q", title="Escuchas", format=",")],
        )
        .properties(height=max(120, 26 * len(rows)))
    )


def week_heatmap(cells: list[tuple[int, int, int]]) -> alt.Chart:
    """Día de la semana × hora del día. Más opaco = más escuchas."""
    peak = max((n for _, _, n in cells), default=0) or 1
    rows = [{"dia": DIAS_CORTOS[d], "dia_largo": DIAS[d].capitalize(), "orden": d, "hora": h,
             "franja": f"{h:02d}:00–{(h + 1) % 24:02d}:00", "escuchas": n} for d, h, n in cells]
    return (
        alt.Chart(alt.Data(values=rows))
        .mark_rect(color=ACCENT, cornerRadius=3)
        .encode(
            x=alt.X("hora:O", title="Hora", scale=alt.Scale(paddingInner=0.12),
                    axis=alt.Axis(labelAngle=0, values=list(range(0, 24, 3)))),
            y=alt.Y("dia:N", title=None, sort=list(DIAS_CORTOS), scale=alt.Scale(paddingInner=0.12)),
            opacity=alt.Opacity("escuchas:Q", scale=alt.Scale(domain=[0, peak], range=[0.06, 1]),
                                legend=None),
            tooltip=[alt.Tooltip("dia_largo:N", title="Día"), alt.Tooltip("franja:N", title="Hora"),
                     alt.Tooltip("escuchas:Q", title="Escuchas", format=",")],
        )
        .properties(height=230)
    )
