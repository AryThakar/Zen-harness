"""Dashboard: period filter, a written briefing, KPI tiles, trend and breakdowns."""

from __future__ import annotations

import html
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import charts
import client
import theme

PRESETS = {"Last 30 days": 30, "Last 90 days": 90, "Last 12 months": 365, "All time": None, "Custom": 0}

KPI_TILES = (  # key, label, formatter, delta colour ("normal": up is good, "inverse": up is bad)
    ("revenue", "Revenue", charts.money, "normal"),
    ("orders", "Orders", charts.count, "normal"),
    ("avg_order_value", "Avg order value", charts.money, "normal"),
    ("profit", "Profit", charts.money, "normal"),
    ("margin_pct", "Margin", charts.pct, "normal"),
    ("customers", "Customers", charts.count, "normal"),
    ("new_customers", "New customers", charts.count, "normal"),
    ("return_rate_pct", "Return rate", charts.pct, "inverse"),
)
LOCKED_BY = {"profit": "unit_cost", "margin_pct": "unit_cost",
             "customers": "customer_id", "new_customers": "customer_id"}


def render() -> None:
    theme.page_header("Dashboard", "How the business is doing - revenue, orders and customers for any period, "
                      "compared with the period before.", eyebrow="Overview")
    try:
        available = client.data_range()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if not available["has_data"]:
        _empty_state()
        return

    data_start = date.fromisoformat(available["start"])
    data_end = date.fromisoformat(available["end"])
    start, end = _period_filter(data_start, data_end)
    if start is None:
        return

    try:
        summary = client.summary(start, end)
        trend = client.trend(start, end, st.session_state.get("dash_grain") or "auto")
        first = next((d for d in ("category", "channel", "location") if summary["available"].get(d)), None)
        leading = client.breakdown(first, start, end, 8) if first else None
    except client.ApiError as exc:
        st.error(str(exc))
        return

    prev = summary["previous_period"]
    if summary["comparison"] == "full":
        compared = f"changes compare with {charts.day(prev['start'])} – {charts.day(prev['end'])}"
    elif summary["comparison"] == "partial":
        compared = (f"no % changes: your data starts {charts.day(data_start)}, so the previous "
                    f"{summary['period']['days']} days are only partly covered")
    else:
        compared = "no % changes: there is no data before this period"
    _briefing(summary, trend, leading, first, start, end)
    st.caption(f"{compared} · data covers {charts.day(data_start)} – {charts.day(data_end)}")
    _kpi_tiles(summary, trend)
    _trend_section(trend, summary)
    _breakdowns(start, end, first, leading)
    _locked_note(summary["locked"])


def _period_filter(data_start: date, data_end: date) -> tuple[date | None, date | None]:
    cols = st.columns([2, 3, 5])
    preset = cols[0].selectbox("Period", list(PRESETS), index=1, key="dash_preset")
    days = PRESETS[preset]
    if preset == "Custom":
        default = (max(data_start, data_end - timedelta(days=89)), data_end)
        picked = cols[1].date_input("From – to", value=default, min_value=data_start, max_value=data_end,
                                    key="dash_custom")
        if not isinstance(picked, (tuple, list)) or len(picked) != 2:
            st.info("Pick an end date.")
            return None, None
        return picked[0], picked[1]
    if days is None:
        return data_start, data_end
    return max(data_start, data_end - timedelta(days=days - 1)), data_end


def _briefing(summary: dict, trend: dict, leading: dict | None, dimension: str | None,
              start: date, end: date) -> None:
    """The period in two plain sentences - every figure straight from the API."""
    k, days = summary["kpis"], summary["period"]["days"]
    esc = html.escape
    first = f"Revenue was <b>{esc(charts.money(k['revenue']['value']))}</b>"
    change = k["revenue"]["change_pct"]
    if change is not None:
        direction = "up" if change >= 0 else "down"
        first += f", <span class='{direction}'>{direction} {abs(change):.1f}%</span> on the previous {days} days"
    first += f", from <b>{esc(charts.count(k['orders']['value']))}</b> orders"
    aov = k["avg_order_value"]["value"]
    first += f" worth <b>{esc(charts.money(aov))}</b> on average." if aov else "."
    extra = []
    if leading and leading["items"] and leading["items"][0]["share_pct"] is not None:
        top = leading["items"][0]
        plural = {"category": "categories"}.get(dimension, f"{dimension}s")
        extra.append(f"<b>{esc(str(top['label']))}</b> leads the {esc(plural)} with "
                     f"{top['share_pct']:.0f}% of revenue")
    full = [p for p in trend["points"] if not p["partial"]]
    if len(full) >= 3:
        best = max(full, key=lambda p: p["revenue"] or 0)
        when = date.fromisoformat(best["bucket"])
        phrase = {"day": f"best day was {charts.day(when)}", "week": f"best week began {charts.day(when)}",
                  "month": f"best month was {when:%B %Y}"}[trend["grain"]]
        extra.append(f"the {esc(phrase)} ({esc(charts.money(best['revenue']))})")
    second = ""
    if extra:
        text = "; ".join(extra) + "."
        second = text[0].upper() + text[1:]
    with theme.card("briefing"):
        st.html(f'<span class="bi-chip">Briefing · {esc(charts.day(start))} – {esc(charts.day(end))}</span>')
        st.html(f'<div class="bi-hero">{first} {second}</div>')


def _spark(points: list[dict], key: str) -> list[float] | None:
    """A KPI's value per full trend bucket, for the tile's sparkline."""
    full = [p for p in points if not p["partial"]]
    if len(full) < 3:
        return None
    if key == "avg_order_value":
        return [p["revenue"] / p["orders"] if p["orders"] else 0.0 for p in full]
    if key == "profit":
        return None if all(p["profit"] is None for p in full) else [p["profit"] or 0.0 for p in full]
    return [float(p[key] or 0) for p in full]


def _kpi_tiles(summary: dict, trend: dict) -> None:
    kpis, locked = summary["kpis"], summary["locked"]
    days = summary["period"]["days"]
    for row, sparks in ((KPI_TILES[:4], True), (KPI_TILES[4:], False)):
        for col, (key, label, fmt, colour) in zip(st.columns(4), row):
            k = kpis[key]
            needs = LOCKED_BY.get(key)
            if needs and needs in locked:
                col.metric(label, "—", help=f"Add a {needs} column to your data to see this.", border=True)
                continue
            delta = charts.pct(k["change_pct"], signed=True) if k["change_pct"] is not None else None
            full = summary["comparison"] == "full"
            help_text = f"Previous {days} days: {fmt(k['previous'])}" if full and k["previous"] is not None else None
            line = _spark(trend["points"], key) if sparks else None
            col.metric(label, fmt(k["value"]), delta=delta, delta_color=colour, border=True, help=help_text,
                       chart_data=line, chart_type="area")


def _trend_section(trend: dict, summary: dict) -> None:
    options = ["Revenue", "Orders"] + ([] if "unit_cost" in summary["locked"] else ["Profit"])
    with theme.card("trend"):
        top = st.columns([4, 3, 3], vertical_alignment="bottom")
        metric = top[1].segmented_control("Show", options, default="Revenue", key="dash_metric") or "Revenue"
        top[2].segmented_control("Group by", ["auto", "day", "week", "month"], default="auto", key="dash_grain",
                                 format_func=lambda g: "Auto" if g == "auto" else g.title())
        key = metric.lower()
        points = trend["points"]
        top[0].subheader(f"{metric} by {trend['grain']}")
        st.plotly_chart(charts.trend_figure(points, key, trend["grain"]), width="stretch",
                        config={"displayModeBar": False}, key="trend_chart")
        if any(p["partial"] for p in points):
            st.caption(f"Open circles mark a {trend['grain']} only partly inside the period, "
                       "so its total is lower than a full one.")
        with st.expander("Show as table"):
            table = pd.DataFrame(points).rename(columns=str.title)
            st.dataframe(table, hide_index=True, width="stretch")


def _breakdowns(start: date, end: date, first: str | None, leading: dict | None) -> None:
    left, right = st.columns(2)
    with left:
        if first:
            with theme.card("bd_first"):
                _breakdown_chart(f"Revenue by {first}", first, start, end, limit=8, slot="first", data=leading)
    with right, theme.card("bd_products"):
        _breakdown_chart("Top products", "product", start, end, limit=8, slot="products")

    try:
        dimensions = client.dimensions()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    left, right = st.columns(2)
    shown = {first, "product", "weekday"}
    choices = [d for d in dimensions if d not in shown and d != "customer_id"]
    with left:
        if choices:
            with theme.card("bd_chosen"):
                default = next((c for c in ("location", "channel") if c in choices), choices[0])
                chosen = st.selectbox("Break down revenue by", choices, index=choices.index(default),
                                      key="dash_dimension")
                _breakdown_chart(None, chosen, start, end, limit=8, slot="chosen")
    with right, theme.card("bd_weekday"):
        _breakdown_chart("Revenue by weekday", "weekday", start, end, limit=7, sort=False, slot="weekday")


def _breakdown_chart(title: str | None, by: str, start: date, end: date, limit: int, slot: str,
                     sort: bool = True, data: dict | None = None) -> None:
    """One breakdown chart. ``slot`` names its place on the page so element keys
    stay unique even when two charts show the same column."""
    if data is None:
        try:
            data = client.breakdown(by, start, end, limit)
        except client.ApiError as exc:
            st.error(str(exc))
            return
    if title:
        st.subheader(title)
    if not data["items"]:
        st.caption("No sales in this period.")
        return
    st.plotly_chart(charts.bar_figure(data["items"], data["other"], sort=sort), width="stretch",
                    config={"displayModeBar": False}, key=f"bar_{slot}")
    with st.expander("Show as table"):
        rows = data["items"] + ([data["other"]] if data["other"] else [])
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _locked_note(locked: dict[str, list[str]]) -> None:
    if not locked:
        return
    lines = [f"- **{col}** → {', '.join(features)}" for col, features in locked.items()]
    st.info("Add these columns to your uploads to unlock more:\n" + "\n".join(lines))


def _empty_state() -> None:
    with theme.card("empty"):
        st.html('<span class="bi-chip">Get started</span>')
        st.html('<div class="bi-hero"><b>No sales data yet.</b> Upload an export from your shop, POS or online '
                'store - messy files are fine - or load 18 months of demo sales to explore every feature.</div>')
        cols = st.columns([1, 1, 3])
        if cols[0].button("Load demo data", type="primary",
                          help="18 months of synthetic sales for an electronics retailer"):
            with st.spinner("Generating 18 months of sales…"):
                try:
                    report = client.load_demo()
                except client.ApiError as exc:
                    st.error(str(exc))
                    return
            st.success(f"Demo data loaded: {report['summary']}")
            st.rerun()
        cols[1].button("Upload a file", on_click=lambda: st.session_state.update(page="Upload data"))
