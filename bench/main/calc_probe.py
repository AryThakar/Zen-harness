"""How does the main model use calculate? 20 everyday maths questions, asked of the real model with
main's real instructions and tools; every expression it writes into calculate is recorded as it was
written (notation, functions, one step or many), with whether it used the tool at all, handed the
question to a helper instead, and got the answer right.

    python -m bench.main.calc_probe          (llama-server running)  -> bench/main/calc_probe.md
    python -m bench.main.calc_probe --real   the real calculate and its description -> calc_probe_real.md

calculate's description is neutral here ("a math expression, e.g. '2400 * 0.15'") so the listed
functions do not steer what the model writes, and the evaluator accepts every function of Python's
math module (plus ln) so a call is not refused for a name: the point is to see what the model
reaches for. Other tools are not run: the first one chosen is recorded and the model is told to use
calculate or answer itself. Runs on the worker slot, so the main slot's cache is left alone.
"""

from __future__ import annotations

import json
import math
import sys
import re
import statistics
import tempfile
import time
from pathlib import Path

from simpleeval import SimpleEval

from app.providers.llamacpp import LlamaCppProvider
from app.assistant.context import context_block
from app.assistant.main import MAIN, stamp
from app.assistant.registry import ToolContext
from app.assistant.workers import main_tools
from app.tools.calc import calculate
from app.tools.workspace import Workspace

HERE = Path(__file__).resolve().parent
NEUTRAL = "a math expression, e.g. '2400 * 0.15'"
MAX_STEPS = 5


def h(*ps: float) -> float:
    return -sum(p * math.log2(p) for p in ps if p)


PROBLEMS = [  # (question, expected numbers that must all appear in the answer)
    ("What is 18% of 2,450?", [441]),
    ("A price went from 1,250 to 1,410. What is the percentage increase?", [12.8]),
    ("What is 7 to the power 5?", [16807]),
    ("What is the square root of 1764?", [42]),
    ("What is the cube root of 2197?", [13]),
    ("What is the natural log of 50?", [math.log(50)]),
    ("What is log base 2 of 1024?", [10]),
    ("What is log10 of 0.001?", [-3]),
    ("What is sin of 30 degrees?", [0.5]),
    ("What is tan of 35 degrees?", [math.tan(math.radians(35))]),
    ("What angle in degrees has a sine of 0.5?", [30]),
    ("What is 10 factorial?", [3628800]),
    ("In how many ways can I choose 3 people from a group of 12?", [220]),
    ("I invest 50,000 at 7.5% a year, compounded monthly. How much do I have after 3 years?",
     [50000 * (1 + 0.075 / 12) ** 36]),
    ("What is the average of 12, 15, 22, 9 and 30?", [17.6]),
    ("What is the population standard deviation of 2, 4, 4, 4, 5, 5, 7, 9?", [2]),
    ("Solve 2x^2 - 7x + 3 = 0.", [3, 0.5]),
    ("What is e to the power -0.5?", [math.exp(-0.5)]),
    ("What is the entropy in bits of a distribution with probabilities 0.5, 0.25 and 0.25?", [1.5]),
    ("Convert 72 degrees Fahrenheit to Celsius.", [(72 - 32) * 5 / 9]),
]

FUNCTIONS = {name: getattr(math, name) for name in dir(math) if callable(getattr(math, name))}
FUNCTIONS.update({"ln": math.log, "abs": abs, "round": round, "min": min, "max": max, "sum": lambda *a: sum(a),
                  "mean": lambda *a: statistics.mean(a), "pstdev": lambda *a: statistics.pstdev(a)})
EVAL = SimpleEval(functions=FUNCTIONS, names={"pi": math.pi, "e": math.e})
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NUMBER = re.compile(r"-?\d[\d,]*\.?\d*(?:[eE]-?\d+)?|-?\.\d+")


def evaluate(expression: str) -> dict:
    expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression.strip()).replace("^", "**")  # as calculate does
    try:
        value = EVAL.eval(expr)
        return {"status": "ok", "expression": expr, "result": float(f"{value:.12g}")}
    except Exception as exc:  # noqa: BLE001 - recorded, and returned to the model like the real tool would
        return {"status": "invalid_arguments", "error": {"message": f"Can't evaluate that expression: {exc}"}}


def right(answer: str, expected: list[float], slack: float = 0.006) -> bool:
    """slack: the absolute difference allowed (0.006 lets a 2-decimal rounding pass)."""
    shown = []
    for raw in NUMBER.findall(answer.replace("−", "-")):
        try:
            shown.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return all(any(abs(s - x) <= max(1e-3 * abs(x), slack) for s in shown) for x in expected)


def notation(expression: str) -> list[str]:
    marks = [label for label, present in (("^", "^" in expression), ("**", "**" in expression),
                                          ("°", "°" in expression), ("%", "%" in expression),
                                          ("π", "π" in expression), ("√", "√" in expression),
                                          ("×/÷", any(c in expression for c in "×÷")),
                                          ("several lines", "\n" in expression.strip()),
                                          ("assignment", bool(re.search(r"\b\w+\s*=[^=]", expression))))
             if present]
    return marks


def main() -> None:
    real = "--real" in sys.argv
    root = Path(tempfile.mkdtemp()) / "ws"
    ws = Workspace(root)
    model = LlamaCppProvider("local", thinking=True, slot=1, max_tokens=4096)
    tools = main_tools(model, ToolContext(ws=ws, index=None, conversation="probe"))
    specs = [tool.spec() for tool in tools.values()]
    for spec in specs:
        if spec["name"] == "calculate" and not real:
            spec["input_schema"]["properties"]["expression"]["description"] = NEUTRAL
    system = f"{MAIN}\n\n{context_block(ws)}"

    rows, calls, used = [], [], {}
    only = {int(a) for a in sys.argv[1:] if a.isdigit()}  # e.g. --real 10: just question 10
    for n, (question, expected) in enumerate(PROBLEMS, start=1):
        if only and n not in only:
            continue
        messages, record = [{"role": "user", "content": f"{stamp()} {question}"}], {"q": question, "calls": [],
                                                                                    "other": None}
        started, answer = time.perf_counter(), ""
        for step in range(MAX_STEPS):
            turn = model.complete(system, messages, specs, allow_tools=step < MAX_STEPS - 1)
            messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                             "provider": model.name, "model": turn.model, "stop": turn.stop})
            if not turn.tool_calls:
                answer = turn.text
                break
            for call in turn.tool_calls:
                if call["name"] == "calculate":
                    expression = str((call["input"] or {}).get("expression", ""))
                    result = (json.loads(calculate(expression).for_model()) if real else evaluate(expression))
                    record["calls"].append({"expression": expression, "status": result["status"],
                                            "result": result.get("result"), "error": result.get("error")})
                    for name in CALL.findall(expression):
                        used[name] = used.get(name, 0) + 1
                else:
                    record["other"] = record["other"] or call["name"]
                    result = {"status": "failed", "error": {"message": "Not available in this test: use calculate "
                                                                       "or answer yourself."}}
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                 "content": json.dumps(result), "is_error": result["status"] != "ok"})
        record.update(answer=answer, right=right(answer, expected), seconds=round(time.perf_counter() - started, 1))
        rows.append(record)
        calls += record["calls"]
        print(f"{n:2d} {'ok ' if record['right'] else 'BAD'} calls={len(record['calls'])} other={record['other']} "
              f"{record['seconds']}s  {[c['expression'] for c in record['calls']]}", flush=True)

    with_calc = sum(1 for r in rows if r["calls"])
    out = ["# How the main model uses calculate", "",
           f"Real model (Gemma 4 E4B, thinking on), main's instructions and tools; calculate described neutrally "
           f"(\"{NEUTRAL}\"). {len(PROBLEMS)} questions.", "",
           f"- Right answers: {sum(r['right'] for r in rows)}/{len(rows)}",
           f"- Used calculate: {with_calc}/{len(rows)}; calls in all: {len(calls)}; refused expressions: "
           f"{sum(c['status'] != 'ok' for c in calls)}",
           f"- Handed to another tool first: {sum(1 for r in rows if r['other'])} "
           f"({', '.join(sorted({r['other'] for r in rows if r['other']}))})",
           f"- Functions written: {', '.join(f'{k} x{v}' for k, v in sorted(used.items(), key=lambda kv: -kv[1]))}",
           f"- Notation: {', '.join(sorted({m for c in calls for m in notation(c['expression'])})) or 'plain'}", "",
           "| # | question | right | expressions written (-> result or error) | other tool | answer |", "|---|---|---|---|---|---|"]
    for n, r in enumerate(rows, start=1):
        exprs = "<br>".join(f"`{c['expression']}` -> " + (str(c["result"]) if c["status"] == "ok"
                                                           else c["error"]["message"][:80]) for c in r["calls"])
        cell = lambda s: " ".join(str(s).split()).replace("|", "\\|")
        out.append(f"| {n} | {cell(r['q'])} | {'yes' if r['right'] else '**no**'} | {cell(exprs) or '(none)'} | "
                   f"{r['other'] or ''} | {cell(r['answer'][:160])} |")
    name = "calc_probe_only.md" if only else "calc_probe_real.md" if real else "calc_probe.md"
    if real:
        out[2] = (f"Real model (Gemma 4 E4B, thinking on), main's instructions and tools, the real calculate and its "
                  f"description. {len(PROBLEMS)} questions.")
    (HERE / name).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"-> bench/main/{name}")


if __name__ == "__main__":
    main()
