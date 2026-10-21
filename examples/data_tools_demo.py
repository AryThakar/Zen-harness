"""Run every data and ML tool on real public datasets, the way the assistant would - but with fixed
arguments and no model, so what you see is exactly what the tools do.

    python examples/data_tools_demo.py [data folder]

The data folder needs pizza_messy.csv (Maven pizza sales exported with a title, ';' and
day-first dates) and marriage_survey_train.csv / marriage_survey_new_people.csv (Fair's 1974
affairs survey, as shipped with statsmodels). Writes examples/data_tools_demo.md.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from app.tools.clean_data import clean_data
from app.tools.forecast import forecast
from app.tools.inspect_data import inspect_data
from app.tools.make_chart import make_chart
from app.tools.ml import predict, train_model
from app.tools.query_data import query_data
from app.tools.stats_test import stats_test
from app.tools.workspace import Workspace

HERE = Path(__file__).resolve().parent
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "bench" / "data" / "locked"
out: list[str] = ["# Data tools on real data", "",
                  "Every call below is a direct tool call with fixed arguments (no AI model). "
                  "Results are what the model would receive.", ""]


def call(label: str, fn, *args, **kwargs):
    t = time.perf_counter()
    r = fn(*args, **kwargs)
    secs = time.perf_counter() - t
    shown = json.dumps({"status": r.status, **r.result}, ensure_ascii=False, indent=1, default=str)
    if len(shown) > 3500:
        shown = shown[:3500] + "\n ... (cut for this page)"
    out.extend([f"### {label}", f"`{fn.__name__}` - {r.status} in {secs:.1f}s", "", "```json", shown, "```", ""])
    print(f"{label}: {r.status} ({secs:.1f}s)")
    if r.status not in ("ok", "partial"):
        raise SystemExit(f"{label} failed: {r.result}")
    return r.result


ws = Workspace(Path(tempfile.mkdtemp()) / "workspace")

out.append("## 1. A messy export: pizza sales")
pizza = ws.add_file(DATA / "pizza_messy.csv")
seen = call("Inspect the raw file", inspect_data, ws, pizza)
answer = seen["questions"][0]["options"][0]["step"] if seen["questions"] else None  # what ask_user would return
steps = seen["suggested_steps"] + ([answer] if answer else [])
clean = call("Clean with the suggested steps (+ the user's answer)", clean_data, ws, pizza, steps)["data"]
call("Top pizzas by revenue (exact)", query_data, ws,
     f"SELECT name, round(sum(quantity * price), 2) AS revenue, sum(quantity) AS pizzas FROM {clean} "
     "GROUP BY name ORDER BY revenue DESC LIMIT 5")
daily = call("Daily revenue, saved as a table", query_data, ws,
             f"SELECT date, sum(quantity * price) AS revenue FROM {clean} GROUP BY date ORDER BY date",
             save=True)["saved_as"]
call("Chart: weekly revenue", make_chart, ws, daily, "line", x="date", y="revenue", time_unit="week")
call("Chart: pizzas sold by category", make_chart, ws, clean, "bar", x="category", y="quantity")
call("Is price different between sizes?", stats_test, ws, clean, "difference", ["price", "size"])
call("Are category and size associated?", stats_test, ws, clean, "association", ["category", "size"])
call("Forecast weekly revenue, 6 weeks", forecast, ws, daily, "date", "revenue", time_unit="week", horizon=6)

out.append("## 2. Prediction: the marriage survey")
survey = ws.add_file(DATA / "marriage_survey_train.csv")
labelled = call("Add a yes/no target, saved as a table", query_data, ws,
                f"SELECT *, CASE WHEN affairs > 0 THEN 'yes' ELSE 'no' END AS had_affair FROM {survey}",
                save=True)["saved_as"]
call("Train with every column (the leak check should object)", train_model, ws, labelled, "had_affair")
model = call("Train without the leaking column", train_model, ws, labelled, "had_affair",
             features=["rate_marriage", "age", "yrs_married", "children", "religious", "educ", "occupation",
                       "occupation_husb"])["model"]
new = ws.add_file(DATA / "marriage_survey_new_people.csv")
call("Predict for new people", predict, ws, model, new)

(HERE / "data_tools_demo.md").write_text("\n".join(out), encoding="utf-8")
print(f"wrote {HERE / 'data_tools_demo.md'}")
