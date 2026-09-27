"""Insights: the ML models - forecast, customer segments, unusual days."""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

import charts
import client

POLL_SECONDS = 0.5
MAX_WAIT_SECONDS = 300
HORIZONS = {"day": [14, 30, 60, 90], "week": [8, 12, 26]}


def render() -> None:
    st.header("Insights")
    st.caption("Machine-learning models run on your own sales. Each result says how far it can be trusted.")
    forecast_tab, segments_tab, anomalies_tab = st.tabs(["Forecast", "Customer segments", "Unusual days"])
    with forecast_tab:
        _forecast()
    with segments_tab:
        _segments()
    with anomalies_tab:
        _anomalies()


def _run(task: str, body: dict | None = None) -> None:
    """Start a run, wait for it, and show why if it failed."""
    try:
        run = client.start_ml(task, body)
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        with st.spinner("Running the model…"):
            while run["status"] == "running" and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
                run = client.ml_run(run["run_id"])
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if run["status"] == "failed":
        st.error(run["message"])
    elif run["status"] == "running":
        st.warning("Still running - check back in a moment.")
    else:
        st.rerun()


def _when(run: dict) -> str:
    finished = datetime.fromisoformat(run["finished_at"])
    return f"run #{run['run_id']}, {finished:%d %b %H:%M}"


def _load(task: str) -> dict | None:
    try:
        return client.ml_result(task)
    except client.ApiError as exc:
        st.error(str(exc))
        return None


# --- forecast ------------------------------------------------------------------

def _forecast() -> None:
    cols = st.columns([2, 2, 2, 3])
    grain = cols[0].segmented_control("Forecast by", ["day", "week"], default="day", key="fc_grain",
                                      format_func=str.title) or "day"
    horizon = cols[1].selectbox("How far ahead", HORIZONS[grain], index=1, key=f"fc_horizon_{grain}",
                                format_func=lambda h: f"{h} {grain}s")
    scope = cols[2].selectbox("What", ["All sales", "category", "product", "location", "channel"], key="fc_scope",
                              format_func=lambda s: s if s == "All sales" else f"One {s}")
    body = {"horizon": horizon, "grain": grain}
    if scope != "All sales":
        try:
            labels = [i["label"] for i in client.breakdown(scope, limit=100)["items"] if i["label"] != "(not set)"]
        except client.ApiError as exc:
            st.error(str(exc))
            return
        if not labels:
            cols[3].caption(f"Your data has no {scope} column.")
            return
        body |= {"by": scope, "value": cols[3].selectbox(scope.title(), labels, key=f"fc_value_{scope}")}
    if st.button("Run forecast", type="primary", key="fc_run"):
        _run("forecast", body)

    result = _load("forecast")
    if result is None:
        st.info("No forecast yet. Choose what to forecast and press **Run forecast**.")
        return
    run, m = result["run"], result["run"]["metrics"]
    unit = f"{m['grain']}s"
    subject = f"{m['by']} '{m['value']}'" if m["by"] else "all sales"
    st.subheader(f"Next {m['horizon']} {unit}: {subject}")
    tiles = st.columns(4)
    tiles[0].metric("Expected revenue", charts.money(m["expected_total"]), border=True,
                    help="Sum of the forecast. The shaded band on the chart is the 80% range for each "
                         f"{m['grain']}.")
    tiles[1].metric("Backtest error", charts.pct(m["error_pct"]), border=True,
                    help="Average miss when forecasting past periods, as a share of actual revenue (WAPE).")
    tiles[2].metric("Baseline error", charts.pct(m["baseline_error_pct"]), border=True,
                    help="The same test for simply repeating the previous period. Lower is better.")
    tiles[3].metric("Range held", charts.pct(m["range_coverage_pct"]), border=True,
                    help="How often the real value fell inside the 80% range in backtests (ideal ≈ 80%).")
    if m["model"] == "baseline":
        verdict = "No model beat simply repeating the previous period, so this forecast does that."
    else:
        verdict = (f"Model: {m['model_label']}, {m['improvement_pct']:.0f}% more accurate than repeating the "
                   f"previous period, tested on {m['backtest_windows']} past periods.")
    st.caption(f"{verdict} Based on {m['history']} {unit} of history ({_when(run)}).")
    st.plotly_chart(charts.forecast_figure(result["history"], result["points"], m["grain"]), width="stretch",
                    config={"displayModeBar": False}, key="forecast_chart")
    with st.expander("Show as table"):
        st.dataframe(pd.DataFrame(result["points"]), hide_index=True, width="stretch")


# --- segments ------------------------------------------------------------------

def _segments() -> None:
    st.caption("Groups customers by how recently they bought, how often, and how much they spent.")
    if st.button("Find segments", type="primary", key="seg_run"):
        _run("segments")
    result = _load("segments")
    if result is None:
        st.info("No segments yet. Press **Find segments** (needs at least 50 customers with a customer_id).")
        return
    run, segments = result["run"], result["segments"]
    st.caption(f"{run['message']} ({_when(run)})")

    left, right = st.columns(2)
    with left:
        st.subheader("Customers per segment")
        items = [{"label": s["segment"], "customers": s["customers"], "share_pct": s["share_pct"]} for s in segments]
        st.plotly_chart(charts.bar_figure(items, value_key="customers", fmt=charts.count), width="stretch",
                        config={"displayModeBar": False}, key="seg_customers")
    with right:
        st.subheader("Share of revenue")
        items = [{"label": s["segment"], "revenue": s["revenue"], "share_pct": s["revenue_share_pct"]} for s in segments]
        st.plotly_chart(charts.bar_figure(items), width="stretch", config={"displayModeBar": False},
                        key="seg_revenue")

    st.subheader("What each segment means")
    for s in segments:
        st.markdown(
            f"**{s['segment']}** · {s['customers']:,} customers · typical customer: "
            f"{round(s['median_recency_days'])} days since last order, "
            f"{s['median_orders']:g} order{'' if s['median_orders'] == 1 else 's'}, "
            f"{charts.money(s['median_revenue'], compact=False)} spent  \n"
            f"{s['meaning']} **→ {s['action']}**"
        )

    chosen = st.selectbox("Customers in", [s["segment"] for s in segments], key="seg_choice")
    try:
        page = client.segment_customers(chosen, limit=100)
    except client.ApiError as exc:
        st.error(str(exc))
        return
    st.caption(f"Top {len(page['rows'])} of {page['total']:,} by revenue")
    rows = pd.DataFrame([{
        "Customer": r["customer_id"], "Days since last order": r["recency_days"], "Orders": r["orders"],
        "Spent": charts.money(r["revenue"], compact=False),
    } for r in page["rows"]])
    st.dataframe(rows, hide_index=True, width="stretch")


# --- anomalies -----------------------------------------------------------------

def _anomalies() -> None:
    st.caption("Days when orders or revenue were far from normal for that weekday and season, "
               "and sale lines that look unusual for their product.")
    if st.button("Check for unusual days", type="primary", key="an_run"):
        _run("anomalies")
    result = _load("anomalies")
    if result is None:
        st.info("Not checked yet. Press **Check for unusual days**.")
        return
    run = result["run"]
    st.caption(f"{run['message']} ({_when(run)})")
    metric = st.segmented_control("Show", ["orders", "revenue"], default="orders", key="an_metric",
                                  format_func=str.title) or "orders"
    st.plotly_chart(charts.anomaly_figure(result["series"], result["days"], metric), width="stretch",
                    config={"displayModeBar": False}, key="anomaly_chart")
    if result["days"]:
        table = pd.DataFrame([{
            "Day": d["day"], "Type": d["direction"],
            "Orders": f"{d['orders']:,} (normal ≈ {d['expected_orders']:.0f})",
            "Revenue": f"{charts.money(d['revenue'])} (normal ≈ {charts.money(d['expected_revenue'])})",
            "Unusual in": d["signal"],
        } for d in result["days"]])
        st.dataframe(table, hide_index=True, width="stretch")
    st.subheader("Unusual sale lines")
    if not result["lines"]:
        st.caption("No sale lines stand out from how their product usually sells.")
        return
    lines = pd.DataFrame(result["lines"])[["order_id", "order_date", "product", "quantity", "unit_price",
                                           "discount", "reason"]]
    lines["order_date"] = pd.to_datetime(lines["order_date"])
    st.dataframe(lines, hide_index=True, width="stretch")
