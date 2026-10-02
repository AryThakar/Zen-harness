"""Chart styling and number formatting.

Palette and mark specs follow the project's data-viz rules: one series hue
(validated for contrast in light and dark mode), recessive hairline grid, 2px
lines, thin bars with rounded ends, values at bar tips, text in text colours
(never the series colour), and one y-axis per chart. Colors and font match the
app's design system (theme.py, .streamlit/config.toml).
"""

from __future__ import annotations

import math
import os
from datetime import date

import plotly.graph_objects as go
import streamlit as st

CURRENCY = os.environ.get("BIZ_CURRENCY", "₹")
FONT = "'Plus Jakarta Sans', system-ui, -apple-system, 'Segoe UI', sans-serif"

# The brand indigo is the one series hue; with the orange accent it passes the
# data-viz validator (lightness band, chroma, CVD separation, >= 3:1 contrast)
# against the card surfaces of each mode: #FBFCFE light, #111829 dark.
TOKENS = {
    "light": {
        "series": "#3E63DD", "series_wash": "rgba(62,99,221,0.10)", "other": "#8B93A7",
        "accent": "#EB6834",
        "text": "#141A2A", "text2": "#4A5468", "muted": "#7A8499",
        "grid": "#E4E8F0", "axis": "#CBD2DE", "surface": "#FBFCFE",
    },
    "dark": {
        "series": "#5472E4", "series_wash": "rgba(84,114,228,0.16)", "other": "#6B7489",
        "accent": "#D95926",
        "text": "#E8ECF6", "text2": "#AEB7CB", "muted": "#7C869C",
        "grid": "#1F2839", "axis": "#2E384D", "surface": "#111829",
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


def number(value: float | None) -> str:
    if value is None:
        return "—"
    return _group(round(value)) if abs(value) >= 100 or value == round(value) else f"{value:.2f}".rstrip("0")


def pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def formatter(unit: str):
    """Value formatter for a chart unit: money, count, percent or number."""
    return {"money": money, "count": count, "percent": pct}.get(unit, number)


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
               sort: bool = True, fmt=None) -> go.Figure:
    """Horizontal bars in one hue (nominal categories never get a value ramp),
    'Other' in grey, value and share at the bar tip."""
    t = TOKENS[mode()]
    rows = list(items) + ([other] if other else [])
    if sort:
        rows = sorted(items, key=lambda r: r[value_key] or 0, reverse=True) + ([other] if other else [])
    labels = [r["label"] for r in rows]
    values = [r[value_key] or 0 for r in rows]
    colors = [t["other"] if other is not None and r is other else t["series"] for r in rows]
    fmt = fmt or (count if value_key in ("orders", "units") else money)
    short = fmt if fmt is not money else (lambda v: money(v, digits=1))
    text = [f"{short(v)} · {r['share_pct']:.0f}%" if r.get("share_pct") is not None else short(v)
            for v, r in zip(values, rows)]
    top = max(values, default=0)

    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker={"color": colors, "cornerradius": 4},
        text=text, textposition="outside", cliponaxis=False,
        textfont={"color": t["text2"], "size": 12},
        customdata=[[fmt(v), f" · {pct(r['share_pct'])} of total" if r.get("share_pct") is not None else ""]
                    for v, r in zip(values, rows)],
        hovertemplate="<b>%{customdata[0]}</b><br>%{y}%{customdata[1]}<extra></extra>",
    ))
    # Room at the right for the tip labels, which are drawn outside the bars.
    fig.update_xaxes(visible=False, range=[min(0, min(values, default=0)), top * 1.6 if top > 0 else 1])
    fig.update_yaxes(autorange="reversed", showgrid=False, linecolor=t["axis"], ticks="",
                     tickfont={"color": t["text"]}, automargin=True)
    fig.update_layout(bargap=0.35)
    return _layout(fig, t, 24 + 34 * max(len(rows), 1))


def _legend(t: dict) -> dict:
    return {"orientation": "h", "x": 0, "y": 1.08, "xanchor": "left", "yanchor": "bottom",
            "font": {"color": t["text2"]}, "bgcolor": "rgba(0,0,0,0)"}


def forecast_figure(history: list[dict], points: list[dict], grain: str) -> go.Figure:
    """Actuals (solid), forecast (dashed, same hue - it's the same measure) and its
    80% range as a wash. One axis; a legend because there are two series."""
    t = TOKENS[mode()]
    word = "Week of" if grain == "week" else "Day"
    fig = go.Figure()
    xs = [p["bucket"] for p in points]
    fig.add_trace(go.Scatter(
        x=xs + xs[::-1], y=[p["upper"] for p in points] + [p["lower"] for p in points][::-1],
        fill="toself", fillcolor=t["series_wash"], line={"width": 0}, hoverinfo="skip",
        name="80% range", showlegend=True,
    ))
    fig.add_trace(go.Scatter(
        x=[h["bucket"] for h in history], y=[h["actual"] for h in history], mode="lines",
        line={"color": t["series"], "width": 2}, name="Actual",
        customdata=[[money(h["actual"]), day(h["bucket"])] for h in history],
        hovertemplate=f"<b>%{{customdata[0]}}</b> actual<br>{word} %{{customdata[1]}}<extra></extra>",
    ))
    # Start the dashed line at the last actual point so the two read as one story.
    join = [{"bucket": history[-1]["bucket"], "predicted": history[-1]["actual"]}] if history else []
    fig.add_trace(go.Scatter(
        x=[p["bucket"] for p in join + points], y=[p["predicted"] for p in join + points], mode="lines",
        line={"color": t["series"], "width": 2, "dash": "dash"}, name="Forecast",
        customdata=[[money(p["predicted"]), day(p["bucket"]),
                     f"{money(p['lower'])} – {money(p['upper'])}" if "lower" in p else "last actual"]
                    for p in join + points],
        hovertemplate=f"<b>%{{customdata[0]}}</b> forecast<br>{word} %{{customdata[1]}}"
                      "<br>80% range %{customdata[2]}<extra></extra>",
    ))
    top = max([h["actual"] for h in history] + [p["upper"] for p in points] + [0])
    ticks = nice_ticks(top)
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], tickfont={"color": t["muted"]})
    fig.update_yaxes(range=[0, ticks[-1]], tickvals=ticks, ticktext=[money(v, digits=1) for v in ticks],
                     gridcolor=t["grid"], zeroline=False, tickfont={"color": t["muted"]})
    _layout(fig, t, 360)
    fig.update_layout(showlegend=True, legend=_legend(t), hovermode="closest", margin={"t": 36})
    return fig


def anomaly_figure(series: list[dict], days: list[dict], metric: str) -> go.Figure:
    """The daily series with unusual days marked in the accent hue and labelled by date."""
    t = TOKENS[mode()]
    fmt = count if metric == "orders" else money
    expected_key = "expected_orders" if metric == "orders" else "expected_revenue"
    fig = go.Figure(go.Scatter(
        x=[s["day"] for s in series], y=[s[metric] for s in series], mode="lines",
        line={"color": t["series"], "width": 1.5}, name=f"Daily {metric}",
        customdata=[[fmt(s[metric]), day(s["day"])] for s in series],
        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>",
    ))
    if days:
        fig.add_trace(go.Scatter(
            x=[d["day"] for d in days], y=[d[metric] for d in days], mode="markers+text",
            marker={"size": 10, "color": t["accent"], "line": {"width": 2, "color": t["surface"]}},
            text=[f"{x.day} {x:%b}" for x in (date.fromisoformat(d["day"]) for d in days)],  # %-d fails on Windows
            textposition=["top center" if d["direction"] == "spike" else "bottom center" for d in days],
            textfont={"color": t["text2"], "size": 11}, cliponaxis=False, name="Unusual day",
            customdata=[[fmt(d[metric]), fmt(d[expected_key]), day(d["day"]), d["direction"]] for d in days],
            hovertemplate="<b>%{customdata[0]}</b> (normal ≈ %{customdata[1]})<br>%{customdata[2]} · "
                          "%{customdata[3]}<extra></extra>",
        ))
    top = max([s[metric] for s in series] + [0])
    ticks = nice_ticks(top * 1.08)
    # Room below zero for the date labels under drops, so they don't sit on the x-axis.
    bottom = -0.12 * ticks[-1] if any(d["direction"] == "drop" for d in days) else 0
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], tickfont={"color": t["muted"]})
    fig.update_yaxes(range=[bottom, ticks[-1]], tickvals=ticks, ticktext=[fmt(v) if metric == "orders" else money(v, digits=1) for v in ticks],
                     gridcolor=t["grid"], zeroline=False, tickfont={"color": t["muted"]})
    _layout(fig, t, 340)
    fig.update_layout(showlegend=True, legend=_legend(t), hovermode="closest", margin={"t": 36})
    return fig


def line_figure(points: list[dict], fmt=money, x_title: str = "") -> go.Figure:
    """A generic single-series line (x = dates or ordered values): 2px line, wash,
    the last value labelled, a hover tooltip per point."""
    t = TOKENS[mode()]
    x = [p["x"] for p in points]
    y = [p["y"] for p in points]
    fig = go.Figure(go.Scatter(
        x=x, y=y, mode="lines+markers" if len(points) <= 2 else "lines",
        line={"color": t["series"], "width": 2}, marker={"size": 8, "color": t["series"]},
        fill="tozeroy", fillcolor=t["series_wash"], connectgaps=False,
        customdata=[[fmt(v), str(v_x)] for v, v_x in zip(y, x)],
        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>",
    ))
    last = next(((px, py) for px, py in zip(reversed(x), reversed(y)) if py is not None), None)
    if last:
        fig.add_trace(go.Scatter(
            x=[last[0]], y=[last[1]], mode="markers+text", text=[fmt(last[1])], textposition="top center",
            marker={"size": 8, "color": t["series"], "line": {"width": 2, "color": t["surface"]}},
            textfont={"color": t["text"], "size": 12}, hoverinfo="skip", cliponaxis=False,
        ))
    values = [v for v in y if v is not None]
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], tickfont={"color": t["muted"]},
                     title={"text": x_title, "font": {"color": t["muted"], "size": 12}},
                     showspikes=True, spikemode="across", spikethickness=1, spikecolor=t["axis"], spikedash="solid")
    fig.update_yaxes(gridcolor=t["grid"], zeroline=False, tickfont={"color": t["muted"]})
    if values and min(values) >= 0:  # round ticks from zero; negative data keeps Plotly's own ticks
        ticks = nice_ticks(max(values) * 1.08)
        fig.update_yaxes(range=[0, ticks[-1]], tickvals=ticks,
                         ticktext=[money(v, digits=1) if fmt is money else fmt(v) for v in ticks])
    fig.update_layout(hovermode="x")
    return _layout(fig, t, 320)
