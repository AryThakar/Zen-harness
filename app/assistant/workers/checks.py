# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The checks a helper's result must pass before main sees it: a web answer read from pages, cited by the pages'
numbers, with its file links in downloads; knowledge sources really returned; every number in the engineer's
answer from a tool result or a run. A check verifies; it never edits the result: a failed one names the problem
and gives one chance to fix it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import SourcedResult, WebResult, WorkResult
    from .evidence import Trace


# An answer that opens by saying it didn't get there ("I could not finish.") claims nothing to check.
GAVE_UP = re.compile(r"\W*(?:I|we)\s+(?:could not|couldn'?t|cannot|can'?t|was unable|wasn'?t able|did not|didn'?t)\b",
                     re.I)


URL = re.compile(r"https?://[^\s)\]>\"'`]+")
MONEY = re.compile(r"([$€£₹¥])\s?\d")  # a currency sign on a number
NO_CURRENCY = "the data has no currency"  # in a helper's warning: main's check (checks.added_currency) reads it too


def _check_web(result: WebResult, trace: Trace, _context: str) -> list[str]:
    from app.tools.web import DATA_TABLE_NOTE, is_file_link
    problems = [f"{d} is not a link any page you read gave; give only links the tools returned"
                for d in result.downloads if not given_link(d, trace)]
    written = [u.rstrip(".,;:") for u in dict.fromkeys(URL.findall(result.answer))]
    problems += [f"{u} is a file's link: give it in downloads, so main can bring it in" for u in written
                 if u not in result.downloads and is_file_link(u) and given_link(u, trace)]
    for s in result.sources:
        if trace.cited(s):
            continue
        if str(s).strip() in trace.found_urls:
            problems.append(f"{s} was only seen as a search snippet; read it with read_webpage or don't cite it")
        elif isinstance(s, int) or str(s).strip().isdigit():
            problems.append(f"there is no page {s}: you read {len(trace.pages)} page(s), numbered 1 to {len(trace.pages)}")
        else:
            problems.append(f"{s} is not a page you read: cite pages by the numbers their reads gave")
    if copied_rows(result.answer) >= COPIED_ROWS and any(DATA_TABLE_NOTE in o for o in trace.outputs):
        # Measured: a web helper copied a month of share prices out of a page into its answer - closes missing,
        # a high below its low - and the chart was drawn from that; the page's table comes in exact instead.
        # Even with the page in downloads (2026-10-17): main took the copied rows, some marked '~' as estimates,
        # typed them into the analyst's task and never brought the table in - so no chart or forecast tool could run.
        problems.append("your answer copies a table's rows from a page: keep only a short summary in the answer (a "
                        "few key figures) and give that page's link in downloads - main brings its table in as data, "
                        "exact" if not result.downloads else
                        "your answer copies a table's rows from a page you gave in downloads: keep only a short "
                        "summary in the answer (a few key figures) - main brings the table in as data, exact")
    if not result.sources and trace.read_urls and not result.question_for_user:
        problems.append("no sources given; list the numbers of the pages you used")
    if (not result.sources and not trace.read_urls and not result.question_for_user
            and not NOTHING_FOUND.search(result.answer) and not GAVE_UP.match(result.answer)):
        # Measured on the 26B: asked for a public data file and an overview of it, the helper found the CSV's
        # link, read nothing, and described "typically such datasets include columns such as ..." - a guess
        # main passed on as the file's columns.
        problems.append("you answered without reading any page: read the best result with read_webpage (a data "
                        "file's link shows its columns and first rows) and answer from what it says")
    return problems


COPIED_ROWS = 8  # lines of three or more numbers each: a table copied out of a page, not a few quoted figures


def copied_rows(text: str) -> int:
    return sum(1 for line in text.splitlines() if len(NUMBER.findall(line)) >= 3)


def given_link(link: str, trace: Trace) -> bool:
    """A file link a tool returned - as given, or the same GitHub file under its other address (a file's page,
    github.com/.../blob/..., is its raw file raw.githubusercontent.com/...: measured, the helper found the page
    and rightly handed on the raw link, and it was refused as a link no page gave)."""
    from app.tools.web import _raw_link
    if any(link in o for o in trace.outputs):
        return True
    raw = _raw_link(link)
    return any(_raw_link(u) == raw for u in trace.found_urls | trace.read_urls)


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
JSON_BREAKS = re.compile(r"\\[nrt]")  # a line break or tab as JSON writes it


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


# Numbers of method, not of data: significance levels, confidence levels, a coin-flip baseline. Measured on the 26B
# (2026-10-09): "the p-value is well above 0.05" was refused as an invented number and cost the analyst a retry.
CONVENTIONS = (0.001, 0.01, 0.05, 0.1, 0.5, 0.9, 0.95, 0.99, 90.0, 95.0, 99.0)


def ungrounded_numbers(answer: str, evidence: list[str]) -> list[str]:
    """Numbers in the answer that no tool result (or the task itself) contains, allowing for the
    rounding the answer shows and for shares written as percentages (0.3003 -> '30%'). Small whole
    numbers (up to 10) pass: 'top 5', '3 groups'; so do the conventions of method (CONVENTIONS). Sizes are
    compared, not signs: "fell 2,220 (1.78%)" is the -2220 and -1.7758 a query gave (measured: refused twice,
    the analyst's chart answer went back as partial); a minus written as '−' isn't read as one anyway."""
    # Evidence is mostly JSON: a line break is the text '\n', and the number after it touched the 'n' - unread
    # (measured 2026-10-17: a run printing the 20 primes one per line, and 19 ... 71 refused as not in any result).
    pool = [abs(v) for text in evidence for _, v, _, _ in numbers_in(JSON_BREAKS.sub(" ", text))]
    missing = []
    for raw, value, tol, percent in numbers_in(answer):
        value = abs(value)
        if value == int(value) and value <= 10 or any(abs(value - c) < 1e-9 for c in CONVENTIONS):
            continue
        if not any(abs(v - value) <= tol or (percent and abs(v * 100 - value) <= tol) for v in pool):
            missing.append(raw)
    return missing


def _check_work(result: WorkResult, trace: Trace, context: str) -> list[str]:
    """The engineer's finish: an answer, and every number in what it says from a tool result, a run's output, the
    code that ran or the task."""
    if result.question_for_user:
        return []
    if not result.answer.strip():
        return ["answer is empty: say what you found or made"]
    said = " ".join(s for s in (result.answer, result.caveats) if s)
    # A program whose latest run failed is not "fixed and running" (measured 2026-10-17: "I fixed the indentation and
    # ran the script successfully" - its last run had failed, and the next run failed again)
    failing = [f"{f} ({why})" for f, why in trace.run_files.items() if why]
    unsaid = [] if not failing or STILL_FAILS.search(said) else [
        f"the latest run of {', '.join(failing)} failed: fix the cause and run it again - or say plainly in your answer "
        "that it doesn't run yet"]
    return unsaid + _numbers_problem(said, trace, context)


# An answer that says the program doesn't run ("fixed the indentation error and ran it" is no such answer)
STILL_FAILS = re.compile(r"\b(?:still (?:fails|failing|raises|errors|broken|has (?:an? )?errors?|doesn'?t)|doesn'?t run"
                         r"|does not run|not (?:yet )?(?:running|working)|couldn'?t (?:fix|get it)|could not (?:fix|get it)"
                         r"|fails (?:with|at|on)|is broken)", re.I)


def _numbers_problem(text: str, trace: Trace, context: str) -> list[str]:
    evidence = trace.outputs + [context] + [code for code, _ in trace.python_runs]
    missing = ungrounded_numbers(text, evidence)
    problems = [f"these numbers are not in any tool result: {', '.join(missing)}; copy numbers exactly from the "
                "results, or compute them with query_data (SELECT arithmetic needs no table)"] if missing else []
    # a currency sign the data never gives (measured 2026-10-22: a cafe's sales with no currency answered in "$" -
    # and main copied it, the helper's answer being its evidence). Only the data and the task count: the model's own
    # scripts printed "$" too, and passed it off as found
    seen = "".join(trace.data_outputs + [context])
    signs = [s for s in dict.fromkeys(MONEY.findall(text)) if s not in seen]
    if signs:
        problems.append(f"{signs[0]} is on your numbers, but {NO_CURRENCY}: write them without it")
    return problems
