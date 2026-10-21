# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Were the main slot's failures the model, or the sampling it ran with? Each failure is replayed
against the raw model - the same prompt and situation as when it happened, none of the code
fixes in the way - N times under the sampling left from the earlier Qwen setup and N times under
Gemma 4 E4B's own (Unsloth's config for this model).

    python -m bench.main.model_checks [N]       (llama-server running; writes bench/main/model_checks.md)

Cases:
    ask_user     after a helper's question: does main call ask_user with a clean list of options?
    stamp        after a failed call: does the reply start with a copy of the user's time stamp?
    currency     relaying a plain number: does main add a currency the data never gave?
    choice       after the user picked 'Day first': does the next helper task carry that choice?
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd

from app.providers import ScriptedProvider
from app.providers.llamacpp import LlamaCppProvider
from app.assistant.main import MAIN
from app.assistant.registry import ToolContext
from app.assistant.workers import main_tools
from app.tools.workspace import Workspace

HERE = Path(__file__).resolve().parent
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
SETTINGS = {"Qwen leftovers (t 0.6, top_k 20)": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
            "Gemma's own (t 1.0, top_k 64)": {"temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0.0}}

# The instructions as they were when the failures happened (before the fixes changed them).
BEFORE = (MAIN.replace(", and don't add currency symbols or units the data doesn't give", "")
          .replace("- When a helper needs the user to decide something, the user is asked for you; their answer comes "
                   "back as the helper's result: give the helper the task again together with that answer.",
                   "- If a helper returns question_for_user, ask it with ask_user (with its options), then give the "
                   "helper the task again together with the user's answer."))
QUESTION = ("The dates in date could be read two ways (e.g. 01/01/2025). Which order do they use?\n\nOptions:\n"
            "1. Day first (03/04/2025 = 3 April)\n2. Month first (03/04/2025 = March 4)")
STAMP = re.compile(r"^\s*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2} \w{3} \d{4}, \d{2}:\d{2}\]")


def asks(content: str) -> list[dict]:
    return [{"role": "user", "content": f"[Sat 03 Oct 2026, 10:00] {content}"}]


def call(cid: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "text": "", "provider": "x", "tool_calls": [{"id": cid, "name": name, "input": args}]}


def result(cid: str, name: str, body: dict) -> dict:
    return {"role": "tool", "tool_call_id": cid, "name": name, "content": json.dumps({"status": "ok", **body})}


HELPER_ASKED = asks("What were the total sales in February 2025?") + [
    call("c1", "analyze_or_code", {"task": "Total sales in February 2025", "data": "data_1"}),
    result("c1", "analyze_or_code", {"worker": "engineer", "answer": "", "question_for_user": QUESTION})]

CASES = {
    "ask_user": HELPER_ASKED,
    "stamp": HELPER_ASKED + [
        call("c2", "ask_user", {"question": "Which order?", "options": "Day first\nMonth first"}),
        {"role": "tool", "tool_call_id": "c2", "name": "ask_user", "is_error": True,
         "content": json.dumps({"status": "invalid_arguments",
                                "error": {"message": "options: Input should be a valid list", "field": "options"}})}],
    "currency": asks("I uploaded our pizza sales. What was the total revenue (quantity times price)?") + [
        call("c1", "analyze_or_code", {"task": "Total revenue: sum of quantity * price", "data": "data_1"}),
        result("c1", "analyze_or_code", {"worker": "engineer", "answer": "The total revenue is 817860.05."})],
    "choice": HELPER_ASKED + [
        call("c2", "ask_user", {"question": QUESTION.split("\n")[0],
                                "options": ["Day first (03/04/2025 = 3 April)", "Month first (03/04/2025 = March 4)"]}),
        result("c2", "ask_user", {"question": QUESTION.split("\n")[0], "answer": "Day first (03/04/2025 = 3 April)"})],
}


def judge(case: str, turn) -> str:
    calls = turn.tool_calls
    if case == "ask_user":
        c = next((c for c in calls if c["name"] == "ask_user"), None)
        if c is None:
            return "no ask_user"
        options = c["input"].get("options") if isinstance(c["input"], dict) else None
        good = isinstance(options, list) and len(options) >= 2 and all(isinstance(o, str) and "<|" not in o for o in options)
        return "ok" if good else "broken options"
    if case == "stamp":
        return "copied the stamp" if STAMP.match(turn.text or "") else "ok"
    if case == "currency":
        text = turn.text or ""
        return "added a currency" if re.search(r"[$₹€£]|\b(USD|INR|EUR|GBP|dollars?|rupees?)\b", text) else (
            "ok" if "817" in text else "no number")
    c = next((c for c in calls if c["name"] == "analyze_or_code"), None)
    if c is None:
        return "did not hand it back"
    task = re.sub(r"[^a-z]", "", str(c["input"]).lower())
    return "ok" if "dayfirst" in task else ("vague (day/month)" if "day" in task else "choice missing")


def main() -> None:
    root = Path(tempfile.mkdtemp())
    ws = Workspace(root)
    ws.add_table(pd.DataFrame({"date": ["01/02/2025"], "amount": [1]}), name="sales_q1.csv")
    tools = main_tools(ScriptedProvider([]), ToolContext(ws=ws, index=None))
    specs = [t.spec() for t in tools.values()]
    system = f"{BEFORE}\n\n{ws.listing()}"
    lines = ["# Main slot failures: the model, or the sampling?", "",
             f"Each case replayed {N} times against the raw model (Gemma 4 E4B, thinking on), with the "
             "instructions as they were when it failed and none of the code fixes in the way.", "",
             "| case | setting | outcomes |", "|---|---|---|"]
    for case, history in CASES.items():
        for name, sampling in SETTINGS.items():
            model = LlamaCppProvider("local", thinking=True, slot=0, max_tokens=4096, sampling=sampling)
            counts: dict[str, int] = {}
            for _ in range(N):
                outcome = judge(case, model.complete(system, history, specs))
                counts[outcome] = counts.get(outcome, 0) + 1
            shown = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: kv[0] != "ok"))
            lines.append(f"| {case} | {name} | {shown} |")
            print(f"{case:9s} {name:34s} {shown}", flush=True)
    (HERE / "model_checks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("-> bench/main/model_checks.md")


if __name__ == "__main__":
    main()
