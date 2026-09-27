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
    tabs = st.tabs(["Forecast", "Customer segments", "Churn risk", "Bought together", "Unusual days"])
    with tabs[0]:
        _forecast()
    with tabs[1]:
        _segments()
    with tabs[2]:
        _churn()
    with tabs[3]:
        _basket()
    with tabs[4]:
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


# --- churn ---------------------------------------------------------------------

RISK_LABEL = {"high": "High (over 50%)", "medium": "Medium (25-50%)", "low": "Low (under 25%)"}


def _churn() -> None:
    st.caption("Which regular customers will probably buy nothing in the coming weeks - learned from who "
               "stopped buying in your own history, and tested on the most recent period.")
    cols = st.columns([3, 2, 4], vertical_alignment="bottom")
    horizon = cols[0].segmented_control("Look ahead", [30, 60, 90], default=90, key="churn_horizon",
                                        format_func=lambda d: f"{d} days") or 90
    if cols[1].button("Predict churn", type="primary", key="churn_run"):
        _run("churn", {"horizon_days": horizon})
    result = _load("churn")
    if result is None:
        st.info("No prediction yet. Press **Predict churn** (needs customer ids and at least three times the "
                "look-ahead period of history).")
        return
    run, m = result["run"], result["run"]["metrics"]
    st.subheader(f"Next {m['horizon_days']} days")
    tiles = st.columns(4)
    tiles[0].metric("Likely to stop buying", charts.count(m["high"]), border=True,
                    help="Regular customers with a better than even chance of buying nothing in the period.")
    tiles[1].metric("Spend at risk", charts.money(m["value_at_risk"]), border=True,
                    help="What these customers would normally spend in the period, weighted by their chance of "
                         "not buying.")
    tiles[2].metric("Ranking quality (AUC)", f"{m['auc']:.2f}", border=True,
                    help="How well the model ranked customers who then stopped buying above those who didn't, on "
                         f"the most recent {m['horizon_days']} days: 0.5 is guessing, 1.0 perfect. Only knowing "
                         f"days since last purchase scored {m['baseline_auc']:.2f}.")
    tiles[3].metric("Found in top 20%", charts.pct(m["top20_capture_pct"]), border=True,
                    help="Share of the customers who actually stopped buying that were in the model's top 20% "
                         f"(random picking: 20%; days since last purchase: {m['baseline_top20_capture_pct']:.0f}%).")
    st.caption(f"{run['message']} ({_when(run)})")

    left, right = st.columns([1, 2])
    with left:
        items = [{"label": RISK_LABEL[b["risk"]], "customers": b["customers"]} for b in result["bands"]]
        st.plotly_chart(charts.bar_figure(items, value_key="customers", sort=False, fmt=charts.count),
                        width="stretch", config={"displayModeBar": False}, key="churn_bands")
    with right:
        risk = st.selectbox("Customers", ["high", "medium", "low"], format_func=lambda r: f"{RISK_LABEL[r]} risk",
                            key="churn_risk")
        try:
            page = client.churn_customers(risk, limit=200)
        except client.ApiError as exc:
            st.error(str(exc))
            return
        st.caption(f"{min(len(page['rows']), page['total']):,} of {page['total']:,}, most at stake first")
    if page["rows"]:
        table = pd.DataFrame([{
            "Customer": r["customer_id"], "Chance of not buying": r["probability"],
            "Days since last purchase": r["recency_days"], "Usually every (days)": round(r["typical_gap_days"]),
            "Usual spend at risk": charts.money(r["value_at_risk"], compact=False), "Warning signs": r["signals"],
        } for r in page["rows"]])
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "Customer": st.column_config.TextColumn(width="small"),
            "Chance of not buying": st.column_config.ProgressColumn(
                "Chance", format="percent", min_value=0, max_value=1, width="small",
                help="Chance of buying nothing in the period"),
            "Days since last purchase": st.column_config.NumberColumn("Days since", width="small",
                                                                      help="Days since their last purchase"),
            "Usually every (days)": st.column_config.NumberColumn("Usual gap", width="small",
                                                                  help="Median days between their purchases"),
            "Usual spend at risk": st.column_config.TextColumn("Spend at risk", width="small"),
            "Warning signs": st.column_config.TextColumn(width="large"),
        })


# --- basket --------------------------------------------------------------------

def _basket() -> None:
    st.caption("Products bought in the same order more often than chance - ideas for bundles, displays and "
               "\"add to cart\" suggestions. Needs real order numbers.")
    if st.button("Find products bought together", type="primary", key="basket_run"):
        _run("basket")
    try:
        result = client.basket(limit=200)
    except client.ApiError as exc:
        st.error(str(exc))
        return
    if result is None:
        st.info("Not analysed yet. Press **Find products bought together**.")
        return
    run, rules = result["run"], result["rules"]
    st.caption(f"{run['message']} ({_when(run)})")
    if not rules:
        return
    products = sorted({r["antecedent"] for r in rules})
    chosen = st.selectbox("What goes with", products, key="basket_item",
                          index=products.index(rules[0]["antecedent"]))
    companions = sorted((r for r in rules if r["antecedent"] == chosen), key=lambda r: -r["confidence_pct"])
    st.subheader(f"Bought with {chosen}")
    items = [{"label": r["consequent"], "share": r["confidence_pct"]} for r in companions]
    st.plotly_chart(charts.bar_figure(items, value_key="share", sort=False, fmt=charts.pct), width="stretch",
                    config={"displayModeBar": False}, key="basket_chart")
    st.caption(f"Share of orders with {chosen} that also include each product. "
               + "; ".join(f"{r['consequent']}: {r['lift']:.1f}x its usual rate" for r in companions[:3]) + ".")
    with st.expander("All pairs"):
        table = pd.DataFrame([{
            "Customers who buy": r["antecedent"], "Also buy": r["consequent"],
            "Share of their orders": f"{r['confidence_pct']:.0f}%", "Times the usual rate": round(r["lift"], 1),
            "Orders together": r["orders_together"],
        } for r in rules])
        st.dataframe(table, hide_index=True, width="stretch")


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
