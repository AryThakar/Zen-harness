"""The checks a helper's result must pass before main sees it: web sources really read, knowledge sources
really returned, every number in a data answer from a tool result, the coder's report true to its code
and its last run. A failed check gives the helper one chance to fix it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.tools.reports import DATA_SECTIONS, section_problems
from app.tools.scratch import report_problems

if TYPE_CHECKING:
    from .contracts import DataReport, SaveResult, SourcedResult, WebResult
    from .evidence import Trace


def _check_web(result: WebResult, trace: Trace, _context: str) -> list[str]:
    problems = [f"{d} is not a link any page you read gave; give only links the tools returned"
                for d in result.downloads if not any(d in o for o in trace.outputs)]
    for s in result.sources:
        if s in trace.read_urls:
            continue
        if s in trace.found_urls:
            problems.append(f"{s} was only seen as a search snippet; read it with read_webpage or don't cite it")
        else:
            problems.append(f"{s} was never found or read; cite only pages you read")
    if not result.sources and trace.read_urls and not result.question_for_user:
        problems.append("no sources given; list the URLs of the pages you used")
    return problems


NOTHING_FOUND = re.compile(r"\b(not|no|couldn'?t|cannot|can'?t|don'?t|doesn'?t|unable|isn'?t)\b.{0,60}"
                           r"\b(find|found|mention|contain|cover|information|available|include)", re.I)


def _check_knowledge(result: SourcedResult, trace: Trace, _context: str) -> list[str]:
    problems = [f"'{s}' is not a source any tool returned; copy sources exactly as given"
                for s in result.sources if not any(s in t or t in s for t in trace.sources)]
    if (not result.sources and trace.sources and not result.question_for_user
            and not NOTHING_FOUND.search(result.answer)):
        problems.append("no sources given; list the sources (exactly as the tools gave them) your answer comes from")
    return problems


NUMBER = re.compile(r"(?<![\w.])([-+]?\d[\d,]*(?:\.\d+)?)(\s?%|\s?(?:K|M|B|bn|thousand|million|billion)\b)?")


SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def numbers_in(text: str) -> list[tuple[str, float, float, bool]]:
    """(as written, value, rounding tolerance, is a percentage) for every number in the text.
    '1,200.50' -> 1200.5 (+-0.005); '1.2M' -> 1,200,000 (+-50,000); '30%' -> 30 (+-0.5)."""
    out = []
    for digits, suffix in NUMBER.findall(text):
        suffix = (suffix or "").strip()
        try:
            base = float(digits.replace(",", ""))
        except ValueError:
            continue
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = SCALE.get(suffix.lower(), 1.0)
        out.append((digits + suffix, base * scale, 0.5 * 10 ** -decimals * scale + 1e-9, suffix == "%"))
    return out


def ungrounded_numbers(answer: str, evidence: list[str]) -> list[str]:
    """Numbers in the answer that no tool result (or the task itself) contains, allowing for the
    rounding the answer shows and for shares written as percentages (0.3003 -> '30%'). Small whole
    numbers (up to 10) pass: 'top 5', '3 groups'."""
    pool = [v for text in evidence for _, v, _, _ in numbers_in(text)]
    missing = []
    for raw, value, tol, percent in numbers_in(answer):
        if value == int(value) and abs(value) <= 10:
            continue
        if not any(abs(v - value) <= tol or (percent and abs(v * 100 - value) <= tol) for v in pool):
            missing.append(raw)
    return missing


def _check_data(result: DataReport, trace: Trace, context: str) -> list[str]:
    """Every section written, and every number in it from a tool result (the method's SQL aside)."""
    if result.question_for_user:
        return []
    return (section_problems(DATA_SECTIONS, result.model_dump())
            + _numbers_problem(" ".join((result.data_used, result.results, result.caveats)), trace, context))


def _numbers_problem(text: str, trace: Trace, context: str) -> list[str]:
    missing = ungrounded_numbers(text, trace.outputs + [context])
    return [f"these numbers are not in any tool result: {', '.join(missing)}; copy numbers exactly from the "
            "results, or compute them with query_data (SELECT arithmetic needs no table)"] if missing else []


CLAIMS = ("purpose", "how_it_works", "inputs_outputs")  # what the file does and prints - not change examples


def _check_save(result: SaveResult, trace: Trace, context: str) -> list[str]:
    """Before the coder's file is saved: a report with every section, code that ran - or, after one
    chance to fix it, saved as it is with the report's facts saying it fails - and no number in what the
    report says the file does or prints that isn't in the code, the task or what a tool returned (a run's
    output, a file read, an image looked at). Measured on 14 real reports: those sections had no such
    number; 'How to run or change it' had one, a legitimate example ('print(fibonacci(45))'), so change
    examples and limits are not checked. (A picture the runs made is shown to the coder by the runner, so
    it needs no check that it was looked at.)"""
    if result.question_for_user:
        return []
    scratch = trace.scratch
    code = result.code if result.code and result.code.strip() else (scratch.code if scratch else "")
    if not code.strip():
        return ["there is no code to save: write it with run_code first"]
    name = result.name.strip().lower()
    python = name.endswith(".py") or "." not in name
    problems = report_problems(result.model_dump(), code, python)
    claims = " ".join(str(getattr(result, k) or "") for k in CLAIMS)
    missing = ungrounded_numbers(claims, trace.outputs + [code, context] + [c for c, _ in trace.python_runs])
    if missing:
        problems.append(f"the report states {', '.join(m.strip(',.;:') for m in missing)}, which is not in the code or "
                        "anything a run printed: "
                        "say only what the code does and its last run showed (run it to show more)")
    last = scratch.last if scratch else None
    if python and not result.code and last and last["code"] == code and not last["ok"]:
        error = next((ln for ln in reversed(last["errors"].splitlines()) if ln.strip()), "an error")
        problems.append(f"the last run failed ({error.strip()[:160]}): fix it with fix_code, or call save again to "
                        "keep it as it is - its report will say it fails")
    return problems
