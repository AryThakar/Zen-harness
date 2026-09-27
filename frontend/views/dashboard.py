"""Dashboard: period filter, KPI tiles, trend and breakdowns."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

import charts
import client

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
    st.header("Dashboard")
    try:
        probe = client.summary()
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if not probe["has_data"]:
        _empty_state()
        return

    data_start = date.fromisoformat(probe["data_range"]["start"])
    data_end = date.fromisoformat(probe["data_range"]["end"])
    start, end = _period_filter(data_start, data_end)
    if start is None:
        return

    try:
        summary = client.summary(start, end)
        trend = client.trend(start, end, st.session_state.get("dash_grain") or "auto")
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
    st.caption(
        f"{charts.day(start)} – {charts.day(end)} · {compared} · "
        f"data covers {charts.day(data_start)} – {charts.day(data_end)}"
    )
    _kpi_tiles(summary)
    _trend_section(trend, summary)
    _breakdowns(start, end, summary)
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


def _kpi_tiles(summary: dict) -> None:
    kpis, locked = summary["kpis"], summary["locked"]
    days = summary["period"]["days"]
    for row in (KPI_TILES[:4], KPI_TILES[4:]):
        for col, (key, label, fmt, colour) in zip(st.columns(4), row):
            k = kpis[key]
            needs = LOCKED_BY.get(key)
            if needs and needs in locked:
                col.metric(label, "—", help=f"Add a {needs} column to your data to see this.", border=True)
                continue
            delta = charts.pct(k["change_pct"], signed=True) if k["change_pct"] is not None else None
            full = summary["comparison"] == "full"
            help_text = f"Previous {days} days: {fmt(k['previous'])}" if full and k["previous"] is not None else None
            col.metric(label, fmt(k["value"]), delta=delta, delta_color=colour, border=True, help=help_text)


def _trend_section(trend: dict, summary: dict) -> None:
    options = ["Revenue", "Orders"] + ([] if "unit_cost" in summary["locked"] else ["Profit"])
    top = st.columns([3, 2])
    metric = top[0].segmented_control("Show", options, default="Revenue", key="dash_metric") or "Revenue"
    top[1].segmented_control("Group by", ["auto", "day", "week", "month"], default="auto", key="dash_grain",
                             format_func=lambda g: "Auto" if g == "auto" else g.title())
    key = metric.lower()
    points = trend["points"]
    st.subheader(f"{metric} by {trend['grain']}")
    st.plotly_chart(charts.trend_figure(points, key, trend["grain"]), width="stretch",
                    config={"displayModeBar": False}, key="trend_chart")
    partial = [p for p in points if p["partial"]]
    if partial:
        st.caption(f"Open circles mark a {trend['grain']} only partly inside the period, "
                   "so its total is lower than a full one.")
    with st.expander("Show as table"):
        table = pd.DataFrame(points).rename(columns=str.title)
        st.dataframe(table, hide_index=True, width="stretch")


def _breakdowns(start: date, end: date, summary: dict) -> None:
    available = summary["available"]
    left, right = st.columns(2)
    with left:
        by = "category" if available.get("category") else "channel" if available.get("channel") else "location"
        if available.get(by):
            _breakdown_chart(f"Revenue by {by}", by, start, end, limit=8)
    with right:
        _breakdown_chart("Top products", "product", start, end, limit=8)

    left, right = st.columns(2)
    try:
        dimensions = client.breakdown("product", start, end, limit=1)["dimensions"]
    except client.ApiError as exc:
        st.error(str(exc))
        return
    choices = [d for d in dimensions if d not in ("product", "weekday", "customer_id")]
    with left:
        if choices:
            default = next((c for c in ("location", "channel") if c in choices), choices[0])
            chosen = st.selectbox("Break down revenue by", choices, index=choices.index(default),
                                  key="dash_dimension")
            _breakdown_chart(None, chosen, start, end, limit=8)
    with right:
        _breakdown_chart("Revenue by weekday", "weekday", start, end, limit=7, sort=False)


def _breakdown_chart(title: str | None, by: str, start: date, end: date, limit: int, sort: bool = True) -> None:
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
                    config={"displayModeBar": False}, key=f"bar_{by}")
    with st.expander("Show as table"):
        rows = data["items"] + ([data["other"]] if data["other"] else [])
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _locked_note(locked: dict[str, list[str]]) -> None:
    if not locked:
        return
    lines = [f"- **{col}** → {', '.join(features)}" for col, features in locked.items()]
    st.info("Add these columns to your uploads to unlock more:\n" + "\n".join(lines))


def _empty_state() -> None:
    st.info("No sales data yet. Upload an export from your shop, POS or store, "
            "or load demo data to explore.")
    cols = st.columns([1, 1, 3])
    if cols[0].button("Load demo data", type="primary", help="18 months of synthetic sales for an electronics retailer"):
        with st.spinner("Generating 18 months of sales…"):
            try:
                report = client.load_demo()
            except client.ApiError as exc:
                st.error(str(exc))
                return
        st.success(f"Demo data loaded: {report['summary']}")
        st.rerun()
    cols[1].button("Upload a file", on_click=lambda: st.session_state.update(page="Upload data"))
