"""The assistant's tools.

Each tool has a pydantic input model (its JSON schema is what the model sees, and
every call is validated against it before running), a handler, and returns
text for the model plus an optional artifact - chart or table data the UI draws.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from simpleeval import InvalidExpression, SimpleEval

from app.analytics import dashboard as analytics
from app.db.connection import Database
from app.ml import results, runner
from app.ml.base import NotEnoughData

from . import sql_guard
from .context import schema_card

log = logging.getLogger(__name__)

CONTENT_ROWS = 40  # rows the model sees; artifacts carry up to sql_guard.MAX_ROWS


@dataclass
class ToolContext:
    db: Database
    currency: str


@dataclass
class ToolOutput:
    content: str
    artifact: dict | None = None
    is_error: bool = False


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[_Input]
    handler: Callable[[ToolContext, _Input], ToolOutput]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": _strip_titles(self.input_model.model_json_schema())}


# --- inputs ----------------------------------------------------------------------

class DescribeInput(_Input):
    table: str | None = Field(None, description="a table name for its columns and sample rows; omit for the overview")


class SqlInput(_Input):
    sql: str = Field(description="one DuckDB SELECT query over the listed tables", max_length=8000)


class SummaryInput(_Input):
    start: date | None = Field(None, description="first day (YYYY-MM-DD); default 90 days before the latest sale")
    end: date | None = Field(None, description="last day (YYYY-MM-DD); default the latest sale")


class CalculateInput(_Input):
    expression: str = Field(description="arithmetic, e.g. (5240000 - 4810000) / 4810000 * 100; "
                                        "functions: round abs min max sqrt log exp", max_length=500)


METRICS: dict[str, tuple[tuple[str, ...], str, Callable[[dict], float]]] = {
    "growth_pct": (("old", "new"), "100 x (new - old) / old", lambda v: 100 * (v["new"] - v["old"]) / abs(v["old"])),
    "margin_pct": (("revenue", "cost"), "100 x (revenue - cost) / revenue",
                   lambda v: 100 * (v["revenue"] - v["cost"]) / v["revenue"]),
    "markup_pct": (("cost", "price"), "100 x (price - cost) / cost", lambda v: 100 * (v["price"] - v["cost"]) / v["cost"]),
    "cagr_pct": (("start", "end", "years"), "100 x ((end / start)^(1 / years) - 1)",
                 lambda v: 100 * ((v["end"] / v["start"]) ** (1 / v["years"]) - 1)),
    "average_order_value": (("revenue", "orders"), "revenue / orders", lambda v: v["revenue"] / v["orders"]),
    "break_even_units": (("fixed_costs", "unit_price", "unit_cost"), "fixed_costs / (unit_price - unit_cost)",
                         lambda v: v["fixed_costs"] / (v["unit_price"] - v["unit_cost"])),
    "roi_pct": (("gain", "cost"), "100 x (gain - cost) / cost", lambda v: 100 * (v["gain"] - v["cost"]) / v["cost"]),
    "customer_lifetime_value": (("average_order_value", "orders_per_year", "years", "margin_pct"),
                                "average_order_value x orders_per_year x years x margin_pct / 100",
                                lambda v: v["average_order_value"] * v["orders_per_year"] * v["years"]
                                * v["margin_pct"] / 100),
    "price_after_discount": (("price", "discount_pct"), "price x (1 - discount_pct / 100)",
                             lambda v: v["price"] * (1 - v["discount_pct"] / 100)),
}


class MetricInput(_Input):
    metric: Literal[tuple(METRICS)] = Field(description="; ".join(f"{k}({', '.join(a)})" for k, (a, _, _) in METRICS.items()))
    values: dict[str, float] = Field(description="the inputs the metric needs, by name")


class ChartInput(_Input):
    chart_type: Literal["line", "bar"] = Field(description="line for trends over time, bar to compare categories")
    sql: str = Field(description="SELECT returning the x column and one numeric y column", max_length=8000)
    x: str = Field(description="column for the x axis (dates for line charts, categories for bar charts)")
    y: str = Field(description="numeric column for the values")
    unit: Literal["money", "count", "percent", "number"] = Field(
        description="what y measures, so axis labels are formatted right (money = amounts in the currency)")
    title: str = Field(description="short chart title, e.g. 'Revenue by month'", max_length=80)


class ForecastInput(_Input):
    horizon: int = Field(30, ge=1, le=180, description="how many days or weeks ahead")
    grain: Literal["day", "week"] = "day"
    by: Literal["category", "product", "location", "channel"] | None = Field(
        None, description="forecast one slice only, together with value")
    value: str | None = Field(None, description="e.g. 'Electronics' when by='category'")


class NoInput(_Input):
    pass


# --- handlers --------------------------------------------------------------------

def describe_data(ctx: ToolContext, args: DescribeInput) -> ToolOutput:
    cur = ctx.db.cursor()
    try:
        if args.table is None:
            return ToolOutput(schema_card(cur))
        table = args.table.lower()
        if table not in sql_guard.ALLOWED_TABLES:
            return ToolOutput(f"Unknown table '{args.table}'. Tables: {', '.join(sorted(sql_guard.ALLOWED_TABLES))}.",
                              is_error=True)
        columns = cur.execute(f"DESCRIBE {table}").fetchall()
        sample = sql_guard.run(cur, f"SELECT * FROM {table} LIMIT 3")
    finally:
        cur.close()
    text = f"{table} columns:\n" + "\n".join(f"- {c[0]} {c[1]}" for c in columns)
    return ToolOutput(text + "\n\nSample rows:\n" + _table_text(sample.columns, sample.rows))


def run_sql(ctx: ToolContext, args: SqlInput) -> ToolOutput:
    cur = ctx.db.cursor()
    try:
        result = sql_guard.run(cur, args.sql)
    finally:
        cur.close()
    shown = f" (first {CONTENT_ROWS} shown)" if len(result.rows) > CONTENT_ROWS else ""
    more = f"; the query matched {result.total_rows:,} rows, only {len(result.rows)} returned" if result.truncated else ""
    content = f"{result.total_rows:,} row(s){shown}{more}.\n" + _table_text(result.columns, result.rows[:CONTENT_ROWS])
    artifact = {"kind": "table", "sql": result.sql, "columns": result.columns,
                "rows": [list(map(_jsonable, r)) for r in result.rows], "total_rows": result.total_rows}
    return ToolOutput(content, artifact)


def business_summary(ctx: ToolContext, args: SummaryInput) -> ToolOutput:
    cur = ctx.db.cursor()
    try:
        period = analytics.resolve_period(cur, args.start, args.end)
        if period is None:
            return ToolOutput("No sales data has been loaded yet.", is_error=True)
        summary = analytics.summary(cur, period)
    finally:
        cur.close()
    prev = summary["previous_period"]
    lines = [f"Period {period.start} to {period.end} ({period.days} days)."]
    if summary["comparison"] == "full":
        lines.append(f"Changes are vs {prev.start} to {prev.end}.")
    else:
        lines.append("No % changes: the data doesn't fully cover the previous period of the same length.")
    labels = {"revenue": "Revenue", "orders": "Orders", "avg_order_value": "Average order value",
              "units": "Units sold", "profit": "Profit", "margin_pct": "Margin %", "customers": "Customers",
              "new_customers": "New customers", "return_rate_pct": "Return rate %"}
    for key, label in labels.items():
        k = summary["kpis"][key]
        if k["value"] is None:
            continue
        change = f" ({k['change_pct']:+.1f}% vs previous {_number(k['previous'])})" if k["change_pct"] is not None else ""
        lines.append(f"- {label}: {_number(k['value'])}{change}")
    if summary["locked"]:
        lines.append("Not available (column missing): " + ", ".join(summary["locked"]))
    return ToolOutput("\n".join(lines))


_evaluator = SimpleEval(
    functions={"round": round, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "log": math.log, "exp": math.exp},
    names={"pi": math.pi, "e": math.e},
)


def calculate(ctx: ToolContext, args: CalculateInput) -> ToolOutput:
    try:
        value = _evaluator.eval(args.expression)
    except ZeroDivisionError:
        return ToolOutput("Division by zero.", is_error=True)
    except (InvalidExpression, SyntaxError, TypeError, ValueError, OverflowError) as exc:
        return ToolOutput(f"Can't evaluate that expression: {exc}", is_error=True)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ToolOutput("The expression must produce a number.", is_error=True)
    return ToolOutput(f"{args.expression} = {_number(value)}")


def business_metric(ctx: ToolContext, args: MetricInput) -> ToolOutput:
    needed, formula, compute = METRICS[args.metric]
    missing = [n for n in needed if n not in args.values]
    if missing:
        return ToolOutput(f"{args.metric} needs: {', '.join(needed)} (missing {', '.join(missing)}).", is_error=True)
    try:
        value = compute(args.values)
    except ZeroDivisionError:
        return ToolOutput(f"{args.metric} is undefined here (a divisor is zero).", is_error=True)
    inputs = ", ".join(f"{n}={args.values[n]:,}" for n in needed)
    return ToolOutput(f"{args.metric} = {_number(value)}  [{formula}; {inputs}]")


def make_chart(ctx: ToolContext, args: ChartInput) -> ToolOutput:
    cur = ctx.db.cursor()
    try:
        result = sql_guard.run(cur, args.sql)
    finally:
        cur.close()
    for column in (args.x, args.y):
        if column not in result.columns:
            return ToolOutput(f"Column '{column}' is not in the query result ({', '.join(result.columns)}).",
                              is_error=True)
    xi, yi = result.columns.index(args.x), result.columns.index(args.y)
    points = [{"x": _jsonable(r[xi]), "y": r[yi]} for r in result.rows]
    if not points:
        return ToolOutput("The query returned no rows, so there is nothing to chart.", is_error=True)
    if not all(isinstance(p["y"], (int, float, Decimal)) or p["y"] is None for p in points):
        return ToolOutput(f"Column '{args.y}' must be numeric.", is_error=True)
    for p in points:
        p["y"] = float(p["y"]) if p["y"] is not None else None
    values = [p["y"] for p in points if p["y"] is not None]
    cut = " (first 200 rows)" if result.truncated else ""
    artifact = {"kind": "chart", "chart_type": args.chart_type, "title": args.title,
                "x": args.x, "y": args.y, "unit": args.unit, "points": points}
    return ToolOutput(
        f"Chart '{args.title}' is shown to the user: {len(points)} points{cut}, {args.y} from "
        f"{_number(min(values))} to {_number(max(values))}. Don't repeat every value in the answer.",
        artifact,
    )


def forecast(ctx: ToolContext, args: ForecastInput) -> ToolOutput:
    if (args.by is None) != (args.value is None):
        return ToolOutput("Give both 'by' and 'value' to forecast one slice, or neither.", is_error=True)
    run, cur = _run_task(ctx, "forecast", args.model_dump())
    try:
        if run["status"] != "succeeded":
            return ToolOutput(run["message"], is_error=True)
        view = results.forecast_view(cur, run)
    finally:
        cur.close()
    m = run["metrics"]
    points = view["points"]
    lines = [run["message"],
             f"Backtest error {m['error_pct']:.1f}% vs {m['baseline_error_pct']:.1f}% for repeating the previous "
             f"period; the 80% range held {m['range_coverage_pct']:.0f}% of the time.",
             f"Per {m['grain']} (predicted, 80% range):"]
    lines += [f"- {p['bucket']}: {_number(p['predicted'])} ({_number(p['lower'])} - {_number(p['upper'])})"
              for p in points[:14]]
    if len(points) > 14:
        lines.append(f"... {len(points) - 14} more; the chart shows all of them.")
    artifact = {"kind": "forecast", "grain": m["grain"], "title": f"Forecast: next {m['horizon']} {m['grain']}s",
                "history": _jsonable(view["history"]), "points": _jsonable(points)}
    return ToolOutput("\n".join(lines), artifact)


def customer_segments(ctx: ToolContext, args: NoInput) -> ToolOutput:
    run, cur = _run_task(ctx, "segments", {})
    try:
        if run["status"] != "succeeded":
            return ToolOutput(run["message"], is_error=True)
        view = results.segments_view(cur, run)
    finally:
        cur.close()
    lines = [run["message"], "Segment: customers (share) | revenue share | typical recency days, orders, spend | action"]
    for s in view["segments"]:
        lines.append(
            f"- {s['segment']}: {s['customers']:,} ({s['share_pct']:.0f}%) | {s['revenue_share_pct']:.0f}% | "
            f"{s['median_recency_days']:.0f}, {s['median_orders']:g}, {_number(s['median_revenue'])} | {s['action']}"
        )
    lines.append("Customer lists: query latest_customer_segments with run_sql.")
    artifact = {"kind": "chart", "chart_type": "bar", "title": "Share of revenue by customer segment",
                "x": "segment", "y": "revenue share", "unit": "percent",
                "points": [{"x": s["segment"], "y": s["revenue_share_pct"]} for s in view["segments"]]}
    return ToolOutput("\n".join(lines), artifact)


def unusual_days(ctx: ToolContext, args: NoInput) -> ToolOutput:
    run, cur = _run_task(ctx, "anomalies", {})
    try:
        if run["status"] != "succeeded":
            return ToolOutput(run["message"], is_error=True)
        view = results.anomalies_view(cur, run)
    finally:
        cur.close()
    lines = [run["message"]]
    for d in view["days"]:
        lines.append(f"- {d['day']} {d['direction']}: orders {d['orders']} (normal ~{d['expected_orders']:.0f}), "
                     f"revenue {_number(d['revenue'])} (normal ~{_number(d['expected_revenue'])}), "
                     f"unusual in {d['signal']}")
    for line in view["lines"][:5]:
        lines.append(f"- line {line['order_id']} {line['product']}: {line['reason']}")
    artifact = {"kind": "anomalies", "title": "Unusual days", "series": _jsonable(view["series"]),
                "days": _jsonable(view["days"])}
    return ToolOutput("\n".join(lines), artifact)


TOOLS: tuple[Tool, ...] = (
    Tool("describe_data", "Tables and columns with their meanings, the data's date range, or one table's columns "
         "and sample rows.", DescribeInput, describe_data),
    Tool("run_sql", "Run one read-only DuckDB SELECT over the sales data and ML result tables; returns up to 200 "
         "rows.", SqlInput, run_sql),
    Tool("business_summary", "Headline KPIs for a period - revenue, orders, average order value, units, profit, "
         "margin, customers, new customers, return rate - with % change vs the previous period of the same "
         "length.", SummaryInput, business_summary),
    Tool("calculate", "Evaluate an arithmetic expression exactly. Use it for every calculation.", CalculateInput,
         calculate),
    Tool("business_metric", "Standard business formulas (growth, margin, markup, CAGR, average order value, "
         "break-even, ROI, customer lifetime value, discounted price).", MetricInput, business_metric),
    Tool("make_chart", "Show the user a line or bar chart of a query result.", ChartInput, make_chart),
    Tool("forecast", "Forecast revenue for the coming days or weeks with an 80% range, for all sales or one "
         "category / product / location / channel. Reports how accurate it was in backtests.", ForecastInput,
         forecast),
    Tool("customer_segments", "Group customers into segments (Champions, Loyal, At risk, Occasional, Lost, New) by "
         "recency, frequency and spend, with each segment's size, revenue share and suggested action.", NoInput,
         customer_segments),
    Tool("unusual_days", "Find days with unusual orders or revenue (spikes and drops) and sale lines with an odd "
         "price, quantity or discount.", NoInput, unusual_days),
)


def execute(ctx: ToolContext, name: str, raw_input) -> ToolOutput:
    """Validate and run one tool call; failures become error results the model can act on."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return ToolOutput(f"Unknown tool '{name}'.", is_error=True)
    try:
        args = tool.input_model.model_validate(raw_input if isinstance(raw_input, dict) else {})
        if not isinstance(raw_input, dict):
            raise ValueError("tool input must be a JSON object")
    except (ValidationError, ValueError) as exc:
        problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()] \
            if isinstance(exc, ValidationError) else [str(exc)]
        return ToolOutput(json.dumps({"INVALID_JSON": json.dumps(raw_input, default=str), "problems": problems}),
                          is_error=True)
    try:
        return tool.handler(ctx, args)
    except (sql_guard.SqlRejected, NotEnoughData, analytics.InvalidQuery) as exc:
        return ToolOutput(str(exc), is_error=True)
    except Exception as exc:  # the model gets a short error; the traceback goes to the log
        log.exception("Tool %s failed", name)
        return ToolOutput(f"The {name} tool failed: {exc}", is_error=True)


# --- helpers ---------------------------------------------------------------------

def _run_task(ctx: ToolContext, task: str, params: dict):
    run_id = runner.run(ctx.db, task, params)
    cur = ctx.db.cursor()
    return results.get_run(cur, run_id), cur


def _table_text(columns: list[str], rows: list[tuple]) -> str:
    if not rows:
        return "(no rows)"
    lines = [" | ".join(columns)]
    lines += [" | ".join(_cell(v) for v in row) for row in rows]
    return "\n".join(lines)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _number(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.4g}"


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _strip_titles(schema):
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


TOOLS_BY_NAME = {t.name: t for t in TOOLS}
TOOL_SPECS = [t.spec() for t in TOOLS]
