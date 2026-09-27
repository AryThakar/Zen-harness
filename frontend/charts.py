"""Chart styling and number formatting.

Palette and mark specs follow the project's data-viz rules: one series hue
(validated for contrast in light and dark mode), recessive hairline grid, 2px
lines, thin bars with rounded ends, values at bar tips, text in text colours
(never the series colour), and one y-axis per chart.
"""

from __future__ import annotations

import math
import os
from datetime import date

import plotly.graph_objects as go
import streamlit as st

CURRENCY = os.environ.get("BIZ_CURRENCY", "₹")
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

TOKENS = {
    "light": {
        "series": "#2a78d6", "series_wash": "rgba(42,120,214,0.10)", "other": "#898781",
        "text": "#0b0b0b", "text2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "surface": "#fcfcfb",
    },
    "dark": {
        "series": "#3987e5", "series_wash": "rgba(57,135,229,0.10)", "other": "#898781",
        "text": "#ffffff", "text2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "surface": "#1a1a19",
    },
}


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:
        return "light"


# --- numbers -------------------------------------------------------------------

def _group(whole: int) -> str:
    """Thousands separators; Indian grouping (12,34,567) for rupees."""
    if CURRENCY != "₹":
        return f"{whole:,}"
    s = str(whole)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + parts + [tail])


def money(value: float | None, compact: bool = True, digits: int = 2) -> str:
    """₹32.45 L / ₹1.20 Cr for rupees, $4.2M style otherwise; exact below that."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    a = abs(value)
    units = ((1e7, " Cr"), (1e5, " L")) if CURRENCY == "₹" else ((1e9, "B"), (1e6, "M"), (1e3, "K"))
    if compact:
        for size, suffix in units:
            if a >= size:
                number = f"{a / size:.{digits}f}"
                if "." in number:
                    number = number.rstrip("0").rstrip(".") if digits < 2 else number
                return f"{sign}{CURRENCY}{number}{suffix}"
    return f"{sign}{CURRENCY}{_group(round(a))}"


def count(value: float | None) -> str:
    return "—" if value is None else _group(round(value))


def pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def day(d: str | date) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {d:%b %Y}"


# --- figures -------------------------------------------------------------------

def nice_ticks(top: float, max_intervals: int = 5) -> list[float]:
    """Round ticks from 0 that just cover ``top``: the smallest 1/2/2.5/5/10 step
    that needs at most ``max_intervals`` intervals (so 40.3L -> 0..50L, not 0..60L)."""
    if top <= 0:
        return [0.0, 1.0]
    magnitude = 10 ** math.floor(math.log10(top / max_intervals))
    # m = 10 always fits, because top / max_intervals < 10 * magnitude.
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if math.ceil(top / (m * magnitude)) <= max_intervals)
    return [round(i * step, 10) for i in range(math.ceil(top / step) + 1)]


def _layout(fig: go.Figure, t: dict, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": FONT, "color": t["text2"], "size": 13},
        showlegend=False,
        hoverlabel={"bgcolor": t["surface"], "bordercolor": t["axis"],
                    "font": {"family": FONT, "color": t["text"]}},
    )
    return fig


def trend_figure(points: list[dict], metric: str, grain: str) -> go.Figure:
    """One series over time: 2px line, 10% wash, open markers on partial buckets,
    the latest full value labelled at the end."""
    t = TOKENS[mode()]
    x = [p["bucket"] for p in points]
    y = [p[metric] if p[metric] is not None else 0 for p in points]
    fmt = count if metric == "orders" else money
    period_word = {"day": "Day", "week": "Week of", "month": "Month of"}[grain]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=y, mode="lines", line={"color": t["series"], "width": 2, "shape": "linear"},
        fill="tozeroy", fillcolor=t["series_wash"],
        customdata=[[fmt(v), day(b), " (partial)" if p["partial"] else ""] for v, b, p in zip(y, x, points)],
        hovertemplate=f"<b>%{{customdata[0]}}</b><br>{period_word} %{{customdata[1]}}%{{customdata[2]}}<extra></extra>",
    ))
    partial = [(b, v) for b, v, p in zip(x, y, points) if p["partial"]]
    if partial:
        fig.add_trace(go.Scatter(
            x=[b for b, _ in partial], y=[v for _, v in partial], mode="markers",
            marker={"symbol": "circle-open", "size": 9, "color": t["series"], "line": {"width": 2}},
            hoverinfo="skip",
        ))
    full = [(b, v) for b, v, p in zip(x, y, points) if not p["partial"]]
    if full:
        last_x, last_y = full[-1]
        fig.add_trace(go.Scatter(
            x=[last_x], y=[last_y], mode="markers+text",
            marker={"size": 8, "color": t["series"], "line": {"width": 2, "color": t["surface"]}},
            text=[fmt(last_y)], textposition="top center",
            textfont={"color": t["text"], "size": 12}, hoverinfo="skip", cliponaxis=False,
        ))
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], tickfont={"color": t["muted"]},
                     showspikes=True, spikemode="across", spikethickness=1, spikecolor=t["axis"],
                     spikedash="solid")
    ticks = nice_ticks(max(y, default=0) * 1.08)  # headroom for the end label
    tick_text = [count(v) if metric == "orders" else money(v, digits=1) for v in ticks]
    fig.update_yaxes(range=[min(0, min(y, default=0)), ticks[-1]], tickvals=ticks, ticktext=tick_text,
                     gridcolor=t["grid"], gridwidth=1, zeroline=False, tickfont={"color": t["muted"]})
    fig.update_layout(hovermode="x")
    return _layout(fig, t, 320)


def bar_figure(items: list[dict], other: dict | None = None, value_key: str = "revenue",
               sort: bool = True) -> go.Figure:
    """Horizontal bars in one hue (nominal categories never get a value ramp),
    'Other' in grey, value and share at the bar tip."""
    t = TOKENS[mode()]
    rows = list(items) + ([other] if other else [])
    if sort:
        rows = sorted(items, key=lambda r: r[value_key] or 0, reverse=True) + ([other] if other else [])
    labels = [r["label"] for r in rows]
    values = [r[value_key] or 0 for r in rows]
    colors = [t["other"] if other is not None and r is other else t["series"] for r in rows]
    fmt = count if value_key in ("orders", "units") else money
    short = count if value_key in ("orders", "units") else (lambda v: money(v, digits=1))
    text = [f"{short(v)} · {r['share_pct']:.0f}%" if r.get("share_pct") is not None else short(v)
            for v, r in zip(values, rows)]
    top = max(values, default=0)

    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker={"color": colors, "cornerradius": 4},
        text=text, textposition="outside", cliponaxis=False,
        textfont={"color": t["text2"], "size": 12},
        customdata=[[fmt(v), pct(r.get("share_pct"))] for v, r in zip(values, rows)],
        hovertemplate="<b>%{customdata[0]}</b><br>%{y} · %{customdata[1]} of total<extra></extra>",
    ))
    # Room at the right for the tip labels, which are drawn outside the bars.
    fig.update_xaxes(visible=False, range=[min(0, min(values, default=0)), top * 1.6 if top > 0 else 1])
    fig.update_yaxes(autorange="reversed", showgrid=False, linecolor=t["axis"], ticks="",
                     tickfont={"color": t["text"]}, automargin=True)
    fig.update_layout(bargap=0.35)
    return _layout(fig, t, 24 + 34 * max(len(rows), 1))
