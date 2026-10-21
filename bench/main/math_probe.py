# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Does main solve a maths problem itself, or hand it away? The user's own maths questions, asked
of the real model with main's real instructions and tools, a few runs each: the first tool it picks
(analyze_or_code / research_web / calculate / none), its calculate calls, whether the answer shows steps
and whether its number is right.

    python -m bench.main.math_probe [label]      (llama-server running) -> bench/main/math_probe_<label>.md

calculate runs for real; other tools are not run - the first one picked is what is measured, and
the model is told to use calculate or answer itself. Runs on the worker slot.
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import time
from pathlib import Path

from app.providers.llamacpp import LlamaCppProvider
from app.assistant.context import context_block
from app.assistant.main import MAIN, STAMP, stamp
from app.assistant.registry import ToolContext
from app.assistant.workers import main_tools
from app.tools.calc import calculate
from app.tools.workspace import Workspace

from .calc_probe import h, right

HERE = Path(__file__).resolve().parent
# Exact answers as the model writes them in LaTeX or text, compared with spaces, backslashes, braces
# and brackets removed (\frac{\pi \ln(2)}{8} -> fracpiln28).
SYMBOLIC = [["frac18", "1/8"], ["fracpi^24", "pi^2/4", "π^2/4", "π²/4"],
            ["fracpiln28", "fracpi8ln2", "piln2/8", "πln2/8", "fracπln28"], []]
# The routing rule as it was before (to measure "before" again with the same scoring).
OLD_RULE = "- calculate for any arithmetic."


def solved(answer: str, expected: list[float], symbolic: list[str]) -> bool:
    """The number to 4 decimals (calc_probe's 2-decimal slack let 0.1404 pass for 0.1449) or its exact form."""
    flat = re.sub(r"[\s\\{}()]|left|right|mathrm|text", "", answer).replace("cdot", "")
    return right(answer, expected, slack=5e-5) or any(s in flat for s in symbolic)
RUNS = 2
MAX_STEPS = 6
PROBLEMS = [
    ("Find the value of the following trigonometric product: P = cos(pi/7) * cos(2pi/7) * cos(3pi/7)", [0.125]),
    ("Evaluate the definite integral: I = integral from 0 to pi of (x * sin(x)) / (1 + cos^2(x)) dx", [math.pi ** 2 / 4]),
    ("Evaluate the definite integral: I = integral from 0 to 1 of ln(1 + x) / (1 + x^2) dx. Solve it without hints.",
     [math.pi * math.log(2) / 8]),
    ("A Decision Tree is evaluating a split on a root node with 100 samples: 50 of Class A, 30 of Class B, 20 of "
     "Class C. A split at X = 4.5 gives a left child of 40 samples (30 A, 8 B, 2 C) and a right child of 60 samples "
     "(20 A, 22 B, 18 C). Calculate the Information Gain Ratio of this split with Shannon entropy (log base 2), "
     "rounded to 4 decimal places.",
     [(h(.5, .3, .2) - .4 * h(.75, .2, .05) - .6 * h(1 / 3, 22 / 60, .3)) / h(.4, .6)]),
]


def main() -> None:
    label = next((a for a in sys.argv[1:] if not a.startswith("--")), "run")
    if "--rescore" in sys.argv:  # score the saved answers again, without asking the model
        rows = json.loads((HERE / f"math_probe_{label}.json").read_text(encoding="utf-8"))
        for row in rows:
            question, expected = next(p for p in PROBLEMS if p[0].startswith(row["q"]))
            row["right"] = solved(row["full"], expected, SYMBOLIC[PROBLEMS.index((question, expected))])
        return report(label, rows)
    rules = MAIN
    if "--old" in sys.argv:  # the routing rule as it was: one calculate bullet, no maths bullet
        rules = MAIN[:MAIN.index("- calculate for any arithmetic")] + OLD_RULE + "\n" + MAIN[MAIN.index("- draw_diagram"):]
    ws = Workspace(Path(tempfile.mkdtemp()) / "ws")
    model = LlamaCppProvider("local", thinking=True, slot=1, max_tokens=4096)
    specs = [tool.spec() for tool in main_tools(model, ToolContext(ws=ws, index=None, conversation="probe")).values()]
    system = f"{rules}\n\n{context_block(ws)}"
    rows = []
    for (question, expected), symbolic in zip(PROBLEMS, SYMBOLIC):
        for run in range(1, RUNS + 1):
            messages, first, calcs, answer = [{"role": "user", "content": f"{stamp()} {question}"}], None, [], ""
            started = time.perf_counter()
            for step in range(MAX_STEPS):
                turn = model.complete(system, messages, specs, allow_tools=step < MAX_STEPS - 1)
                messages.append({"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "raw": turn.raw,
                                 "provider": model.name, "model": turn.model, "stop": turn.stop})
                if not turn.tool_calls:
                    answer = STAMP.sub("", turn.text, count=1)
                    break
                for call in turn.tool_calls:
                    first = first or call["name"]
                    if call["name"] == "calculate":
                        res = json.loads(calculate(str((call["input"] or {}).get("expression", ""))).for_model())
                        calcs.append(f"{call['input'].get('expression')} -> {res.get('result', 'refused')}")
                    else:
                        res = {"status": "failed", "error": {"message": "Not available in this test: use calculate "
                                                                         "or answer yourself."}}
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": json.dumps(res), "is_error": res["status"] != "ok"})
            steps = sum(answer.count(m) for m in ("Step", "step ", "\n1.", "$$", "\\int", "=")) >= 4
            row = {"q": question[:60], "run": run, "first": first or "(none)", "calcs": calcs, "steps": steps,
                   "right": solved(answer, expected, symbolic), "seconds": round(time.perf_counter() - started, 1),
                   "answer": " ".join(answer.split())[:220], "full": answer}
            rows.append(row)
            print(f"{row['q'][:40]:<40} run{run} first={row['first']:<13} calcs={len(calcs)} steps={steps} "
                  f"right={row['right']} {row['seconds']}s", flush=True)
    report(label, rows)


def report(label: str, rows: list[dict]) -> None:
    out = [f"# Maths routing: {label}", "",
           f"First tool: {', '.join(f'{k} x{sum(r['first'] == k for r in rows)}' for k in sorted({r['first'] for r in rows}))}"
           f" · right (number found in the answer): {sum(r['right'] for r in rows)}/{len(rows)} · showed steps: "
           f"{sum(r['steps'] for r in rows)}/{len(rows)}", "",
           "| question | run | first tool | calculate calls | steps | right | s | answer |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        cell = lambda s: " ".join(str(s).split()).replace("|", "\\|")
        out.append(f"| {cell(r['q'])} | {r['run']} | {r['first']} | {cell('; '.join(r['calcs'])) or '-'} | "
                   f"{'yes' if r['steps'] else 'no'} | {'yes' if r['right'] else '**no**'} | {r['seconds']} | "
                   f"{cell(r['answer'])} |")
    (HERE / f"math_probe_{label}.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    (HERE / f"math_probe_{label}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> bench/main/math_probe_{label}.md")


if __name__ == "__main__":
    main()
