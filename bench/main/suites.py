"""The cases bench/main/tool_probe.py asks the real model, by suite: one suite per tool of main's
own, written from what users asked in real chats (and the readings a small model gets wrong).

A case's check gets what the turn did (Seen) and returns the problem, or None when the turn did what
it should. Most cases accept every good way of answering - the check is about the mistake that matters.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.assistant.store import Store
from app.config import settings
from app.tools.workspace import Workspace

ZEN, USER = settings.assistant_name, settings.assistant_user_name or "Ary"  # the persona's names (prompts.py)

Check = Callable[["Seen"], "str | None"]


@dataclass
class Seen:
    """What one turn did: every tool that ran (main's and its helpers', in order), the reply."""
    say: str
    started: datetime
    tools: list[dict] = field(default_factory=list)  # {"by": "main" | helper, "name", "input", "status"}
    artifacts: list[dict] = field(default_factory=list)
    answer: str = ""
    asked: dict | None = None  # the question the turn stopped on, waiting for the user
    error: str | None = None
    ws: Workspace | None = None

    @property
    def main(self) -> list[str]:
        return [t["name"] for t in self.tools if t["by"] == "main"]

    def call(self, name: str) -> dict | None:
        """The first call of main's to this tool."""
        return next((t for t in self.tools if t["by"] == "main" and t["name"] == name), None)


@dataclass
class Case:
    id: str
    say: str
    check: Check
    setup: Callable[[Store, str], None] | None = None  # (store, workspace id), before the turn
    history: list[tuple[str, str]] = field(default_factory=list)  # earlier (user, assistant) turns


# ---------------------------------------------------------------- checks

def uses(name: str, test: Callable[[dict], str | None] | None = None) -> Check:
    """Main called this tool (first among its own), it ran, and its input passes the test."""
    def check(s: Seen) -> str | None:
        call = s.call(name)
        if call is None:
            return f"didn't call {name} (called: {', '.join(s.main) or 'nothing'})"
        if s.main[0] != name:
            return f"called {s.main[0]} before {name}"
        if call.get("status") not in ("ok", "partial", "asked"):  # asked: waiting for the user's answer
            return f"{name} came back {call.get('status')}"
        return test(call["input"] or {}) if test else None
    return check


def avoids(*names: str) -> Check:
    def check(s: Seen) -> str | None:
        wrong = [n for n in s.main if n in names]
        return f"called {', '.join(wrong)}" if wrong else None
    return check


def no_tool(s: Seen) -> str | None:
    return f"called {', '.join(s.main)}" if s.main else None


def says(*patterns: str) -> Check:
    """The reply matches every pattern (regular expressions, case-insensitive)."""
    def check(s: Seen) -> str | None:
        missing = [p for p in patterns if not re.search(p, s.answer, re.I)]
        return f"reply lacks {', '.join(missing)}" if missing else None
    return check


def never_says(*patterns: str) -> Check:
    def check(s: Seen) -> str | None:
        found = [p for p in patterns if re.search(p, s.answer, re.I)]
        return f"reply says {', '.join(found)}" if found else None
    return check


def all_of(*checks: Check) -> Check:
    def check(s: Seen) -> str | None:
        return next((p for p in (c(s) for c in checks) if p), None)
    return check


def answered(s: Seen) -> str | None:
    if s.error:
        return f"turn failed: {s.error}"
    return None if s.answer.strip() or s.asked else "no reply"


def the_time(s: Seen) -> str | None:
    """The reply gives the time of the user's message (24 h or 12 h, a minute either way)."""
    t = s.started
    shown = {f"{t.hour}:{t.minute + d:02d}" for d in (-1, 0, 1)} | {
        f"{(t.hour % 12) or 12}:{t.minute + d:02d}" for d in (-1, 0, 1)}
    return None if any(x in s.answer for x in shown) else f"reply lacks the time ({t:%H:%M})"


def the_date(s: Seen) -> str | None:
    """The reply gives the day of the month of the user's message."""
    day = s.started.day
    return None if re.search(rf"\b0?{day}(st|nd|rd|th)?\b", s.answer) else f"reply lacks the day ({day})"


RAW = (r"invalid_arguments", r"\{\s*\"", r"ToolError", r"\bstatus\b", r"Traceback")  # raw errors shown as they are


def field_has(key: str, pattern: str) -> Callable[[dict], str | None]:
    def test(args: dict) -> str | None:
        return None if re.search(pattern, str(args.get(key) or ""), re.I) else f"{key}={args.get(key)!r}"
    return test


def covers(days_ahead: int) -> Check:
    """The weather card's forecast reaches this many days after the user's message (whatever the
    tool's days means)."""
    def check(s: Seen) -> str | None:
        from datetime import timedelta
        wanted = (s.started + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        dates = [d.get("date") for a in s.artifacts if a.get("type") == "weather" for d in a.get("daily", [])]
        return None if wanted in dates else f"forecast doesn't reach {wanted} (has {', '.join(dates) or 'none'})"
    return check


# ---------------------------------------------------------------- setups

CODE = ("Here is the code:\n\n```python\nimport numpy as np\nx = np.linspace(-5, 5, 100)\nprint((x ** 2).max())\n```")


def worker_setup(name: str) -> Callable[[Store, str], None]:
    """A setup of the worker benchmark's (documents, notes, earlier chats; or a table)."""
    def setup(store: Store, ws_id: str) -> None:
        from bench.workers import tasks
        getattr(tasks, name)(*store.open(ws_id), {})
    return setup


def drawn(kind: str, test: Callable[[dict], str | None] | None = None) -> Check:
    """draw_diagram ran and took the code as this kind of diagram."""
    def check(s: Seen) -> str | None:
        problem = uses("draw_diagram", test)(s)
        if problem:
            return problem
        shown = next((a for a in s.artifacts if a.get("type") == "diagram"), {})
        given = str(shown.get("kind", ""))  # Mermaid's own names: sequenceDiagram, erDiagram ...
        return None if given.startswith(kind) else f"drew a {given}, not a {kind}"
    return check


def asks(s: Seen) -> str | None:
    """Asked the user - with ask_user, or as a question in the reply."""
    return None if s.asked or s.answer.rstrip().endswith("?") or "?" in s.answer[-300:] else "didn't ask"


def some_files(store: Store, ws_id: str) -> None:
    """A workspace like the user's: a script, a table, a document, an old text file - and 4 more, so
    the message's snapshot (5 newest) doesn't show everything."""
    ws, index = store.open(ws_id)
    ws.add_bytes("file", b"import numpy as np\nx = np.linspace(-5, 5, 100)\nprint((x ** 2).max())\n", "py",
                 name="parabola.py")
    ws.add_bytes("file", b"remove me", "txt", name="old.txt")
    ws.add_table(pd.DataFrame({"month": ["Jan", "Feb", "Mar"], "sales": [120, 135, 150]}), "sales.csv")
    for n in range(4):
        ws.add_bytes("file", f"draft {n}".encode(), "txt", name=f"draft_{n}.txt")


def remembers(pattern: str) -> Check:
    """main wrote agent.md (files write), and its new text holds the pattern."""
    def check(s: Seen) -> str | None:
        wrote = [t for t in s.tools if t["by"] == "main" and t["name"] == "files" and t.get("status") == "ok"
                 and (t.get("input") or {}).get("mode") == "write"
                 and re.fullmatch(r"agent\.md", str((t.get("input") or {}).get("name") or ""), re.I)]
        if not wrote:
            return f"no files write to agent.md (calls: {', '.join(s.main) or 'none'})"
        return None if re.search(pattern, str(wrote[-1]["input"].get("content") or ""), re.I) else \
            f"agent.md's new text lacks {pattern}"
    return check


def files_call(mode: str, check: Callable[[dict], str | None] | None = None) -> Check:
    return uses("files", lambda a: (None if a.get("mode") == mode else f"mode={a.get('mode')!r}")
                or (check(a) if check else None))


def called_with(name: str, **patterns: str) -> Check:
    """Some call of main's to this tool (not necessarily the first: reading before changing is fine)
    whose inputs match every pattern."""
    def check(s: Seen) -> str | None:
        for t in s.tools:
            args = t.get("input") or {}
            if t["by"] == "main" and t["name"] == name and all(re.search(p, str(args.get(k) or ""), re.I)
                                                               for k, p in patterns.items()):
                return None
        return f"no {name} call with {patterns} (calls: {', '.join(s.main) or 'none'})"
    return check


def with_report(store: Store, ws_id: str) -> None:
    some_files(store, ws_id)
    ws, _ = store.open(ws_id)
    ws.set_report("file_1", "# Plots y = x squared from -5 to 5 and prints its highest value (25.0)\n\n"
                            "## How it works\nnumpy.linspace makes 100 points; the script prints the maximum of x**2.")


def coder_made(store: Store, ws_id: str) -> None:
    """squares.py as the coder would have kept it: the assistant's file, with its report."""
    ws, index = store.open(ws_id)
    from app.tools.files import index_report
    item = ws.add_bytes("file", b"for n in range(1, 11):\n    print(n * n)\n", "py", name="squares.py",
                        meta={"by": "assistant"})
    ws.set_report(item, "# Prints the squares of 1 to 10\n\n## How it works\nA for loop over range(1, 11) prints n * n.\n\n"
                        "## Facts (written by the app)\n- Last run: passed in 0.1 s\n- It printed: 1 4 9 16 25 36 49 64 81 100\n")
    index_report(ws, index, item)


def saved_file(name_pattern: str, printed: str | None = None) -> Check:
    """The coder kept a file matching the name, with a report (and, for Python, what it printed)."""
    def check(s: Seen) -> str | None:
        found = [i for i in (s.ws.ids("file") if s.ws else []) if re.search(name_pattern, s.ws.entry(i)["name"], re.I)]
        if not found:
            return f"no saved file like {name_pattern}"
        report = s.ws.report(found[-1]) or ""
        if not report:
            return "the file has no report"
        return None if printed is None or printed in report else f"report doesn't show {printed!r}"
    return check


def report_kept(table: str) -> Check:
    """The analyst kept its report on the table, and the UI got the report card."""
    def check(s: Seen) -> str | None:
        if not (s.ws and s.ws.report(table)):
            return f"no report on {table}"
        return None if any(a.get("type") == "report" for a in s.artifacts) else "no report card"
    return check


def no_pasted_code(s: Seen) -> str | None:
    return "the reply pastes code" if "```" in s.answer else None


def asked_for(*actions: str) -> Check:
    def check(s: Seen) -> str | None:
        got = ((s.asked or {}).get("approval") or {}).get("action") if s.asked else None
        return None if got in actions else f"no {' or '.join(actions)} approval card (asked: {s.asked})"
    return check

# ---------------------------------------------------------------- suites

SUITES: dict[str, list[Case]] = {
    "weather": [
        Case("here_now", "what's the weather like?", all_of(uses("get_weather", lambda a: None if not a.get("location")
                                                                     else f"location={a['location']!r}"), answered)),
        Case("rain_tomorrow", "Is it going to rain in Mumbai tomorrow?",
             all_of(uses("get_weather", field_has("location", "mumbai")), covers(1), avoids("research_web"))),
        Case("weekend", "weather this weekend in Pune?", all_of(uses("get_weather"), covers(1), avoids("research_web"))),
        Case("air", "how is the air quality today", uses("get_weather")),
        Case("umbrella", "should I carry an umbrella today?", uses("get_weather")),
        Case("time_now", "what is the current time", all_of(no_tool, the_time)),
        Case("time_typo", "what is currunt time", all_of(no_tool, the_time)),
        Case("date", "what's today's date?", all_of(no_tool, the_date)),
        Case("time_tokyo", "what time is it in Tokyo right now?", all_of(answered, says(r"\d{1,2}:\d{2}"))),
        Case("explain", "explain how weather forecasts are made", all_of(no_tool, answered)),
    ],
    "memory": [  # agent.md: what main keeps in view in every chat, changed with files write
        Case("fiscal", "remember that our fiscal year starts in April", remembers(r"april")),
        Case("names", "your name is Zen and my name is Ary, remember that", remembers(r"\bzen\b|\bary\b")),
        Case("always", "keep this in view in every chat: always answer in metric units", remembers(r"metric")),
        Case("save_code", "now save this code", files_call("write", field_has("content", r"linspace")),
             history=[("show me code for y = x^2", CODE)]),
        Case("no_note", "what's 12 times 12?", lambda s: "wrote agent.md" if remembers(r".")(s) is None else None),
        Case("your_name", "what is your name", all_of(says(rf"\b{ZEN}\b"),
                                                      never_says(r"don'?t have a (personal )?name"))),
        Case("my_name", "what is my name?", all_of(says(rf"\b{USER}\b"), never_says(rf"your name is {ZEN}"))),
        Case("who_talking", "who am I talking to?", all_of(says(rf"\b{ZEN}\b"), never_says(rf"you are {ZEN}"))),
    ],
    "diagram": [
        Case("flowchart", "draw a flowchart of how a user logs into a website", drawn("flowchart")),
        Case("sequence", "show a sequence diagram of a browser loading a page from a web server that reads a database",
             drawn("sequence")),
        Case("class", "make a class diagram for a library system with books, members and loans", drawn("class")),
        Case("state", "draw the states of an online order from placed to delivered", drawn("state")),
        Case("er", "draw an ER diagram for a blog with users, posts and comments", drawn("er")),
        Case("gantt", "make a gantt chart for a 3 week website project: design, build, test", drawn("gantt")),
        Case("pie", "show a pie chart of my day: sleep 8h, work 9h, commute 2h, rest 5h", drawn("pie")),
        Case("mindmap", "make a mind map of machine learning topics", drawn("mindmap")),
        Case("timeline", "draw a timeline of Python's major releases", drawn("timeline")),
        Case("colours", "draw a flowchart of making tea, with the start step green and the last step red",
             drawn("flowchart", field_has("mermaid", r"(style|classDef)\b[^\n]*fill"))),
        Case("jk", "give me visulised ans about hows JK flip flop works", all_of(avoids("generate_image"),
                                                                                 uses("draw_diagram"))),
        Case("tree", "now give me visualised ans that shows desicion tree algo rithem", uses("draw_diagram")),
    ],
    "image": [
        Case("cat", "create an image of a cat playing the piano", uses("generate_image", field_has("prompt", "cat"))),
        Case("lion", "give me a img when lion stand with 2 legs", uses("generate_image", field_has("prompt", "lion"))),
        Case("wide", "make a wide picture of a sunset over mountains",
             uses("generate_image", lambda a: None if a.get("size") == "wide" else f"size={a.get('size')}")),
        Case("circuit", "draw an image of a JK flip flop circuit", all_of(avoids("generate_image"), answered)),
        Case("no_image", "describe what a sunset looks like", avoids("generate_image")),
    ],
    "ask": [
        Case("vague", "convert it to the other format", all_of(no_tool, asks)),
        Case("typo", "waht is teh captial of frnace", all_of(avoids("ask_user"), says("paris"))),
        Case("trip", "plan a trip for me", asks),
        Case("clear", "write a haiku about rain", all_of(no_tool, answered)),
    ],
    "import": [
        Case("raw_csv", "import this data: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/tips.csv",
             all_of(files_call("download"), asked_for("import_table", "download_file"))),
        Case("blob", "load https://github.com/mwaskom/seaborn-data/blob/master/iris.csv into my workspace",
             all_of(files_call("download"), asked_for("import_table", "download_file"))),
        Case("repo_page", "get the penguins data from https://github.com/mwaskom/seaborn-data",
             all_of(called_with("files", mode="download", link=r"penguins"), asked_for("import_table", "download_file"))),
    ],
    "files": [
        Case("list", "give me list of all workspace files", all_of(says("parabola", "old.txt", "sales", "draft_0")),
             setup=some_files),
        Case("exists", "do I have a file called budget.xlsx?", all_of(avoids("research_web"), says(r"\bno\b|not|don'?t")),
             setup=some_files),
        Case("read", "show me what is inside parabola.py", all_of(files_call("read"), says("linspace")), setup=some_files),
        Case("write_md", "save this as sales_report.md: Sales rose from 120 in January to 150 in March.",
             files_call("write", field_has("name", r"sales_report\.md")), setup=some_files),
        Case("docx", "make a Word document with a 3 point plan for learning Python",
             files_call("write", field_has("name", r"\.docx$")), setup=some_files),
        Case("delete", "delete old.txt", all_of(files_call("delete"), asked_for("delete_files")), setup=some_files),
        Case("download", "download this file: https://github.com/psf/requests/blob/main/src/requests/__version__.py",
             all_of(files_call("download"), asked_for("download_file")), setup=some_files),
        Case("write_whole", "change old.txt so it only says: keep me",
             all_of(called_with("files", mode="write", content=r"keep me"), asked_for("replace_file")),
             setup=some_files),
        Case("edit_line", "in parabola.py, change the linspace line to use 200 points instead of 100",
             called_with("files", mode="edit", new_text=r"200"), setup=some_files),
        Case("rename", "rename old.txt to archive.txt", called_with("files", mode="rename", new_name=r"archive"),
             setup=some_files),
        Case("report", "what does parabola.py do?", all_of(called_with("files", mode="read"), says(r"25|highest|max")),
             setup=with_report),
    ],
    "coder": [  # run with --helpers: main and the real coder through the real turn
        Case("new_script", "write a python script that prints the first 10 square numbers",
             all_of(uses("analyze_or_code"), saved_file(r"\.py$", "100"), no_pasted_code, answered)),
        Case("change_it", "change squares.py so it prints cubes instead",
             all_of(called_with("analyze_or_code", files=r"squares"), saved_file(r"^squares\.py$", "1000"), no_pasted_code),
             setup=coder_made),
        Case("explain_it", "what does squares.py do?", all_of(avoids("analyze_or_code"), says(r"square"), no_pasted_code),
             setup=coder_made),
        Case("rust", "write rust code that prints 1 to 10", all_of(uses("analyze_or_code"), saved_file(r"\.rs$"), answered)),
    ],
    "analyst": [  # run with --helpers: main and the real data analyst through the real turn
        Case("total", "what were the total sales in pizza_sales? revenue is quantity times price",
             all_of(uses("analyze_or_code"), says(r"817,?860"), report_kept("data_1")), setup=worker_setup("_pizza")),
        Case("best_day", "which day of the week has the most orders in pizza_sales?",
             all_of(uses("analyze_or_code"), says(r"friday"), report_kept("data_1")), setup=worker_setup("_pizza")),
    ],
    "persona": [  # main's persona (prompts.main_prompt): who it is, its manner, how it takes a failure
        Case("hello", "hi", all_of(no_tool, answered, never_says(r"\[|\{"))),
        Case("who_are_you", "who are you? are you chatgpt?",
             all_of(no_tool, says(rf"\b{ZEN}\b"), never_says(r"\bI(?:'m| am) (?:ChatGPT|Claude|Gemini)\b"))),
        Case("bad_place", "what's the weather in Qwxzplorvia right now?",
             all_of(answered, never_says(*RAW), says(r"couldn'?t|could not|can'?t|cannot|(?:was|were)n'?t able|not able|not find|didn'?t find|"
                                                     r"no (?:place|location)|unable|not (?:a )?(?:real|known)"))),
        Case("missing_file", "open budget_2025.xlsx and tell me the total",
             all_of(answered, never_says(*RAW), says(r"\bno\b|not|couldn'?t|can'?t|don'?t"))),
        Case("you_were_wrong", "that's wrong, you messed it up",
             all_of(no_tool, says(r"\b144\b"), never_says(r"I (?:deeply|sincerely|truly) apologi[sz]e")),
             history=[("what is 12 times 12?", "12 times 12 is 124.")]),
        Case("upset", "ugh my laptop died and I lost a day of work",
             all_of(no_tool, answered, never_says(r"^\s*(?:Sure|Certainly)\b"))),
    ],
    "routing": [  # the helpers are stand-ins here: which one main picks, and the task it writes
        Case("web_news", "who won the most recent cricket world cup?", uses("research_web")),
        Case("web_release", "what's new in the latest llama.cpp release?", uses("research_web")),
        Case("doc", "can a customer return an opened laptop?", uses("ask_knowledge"), setup=worker_setup("_knowledge")),
        Case("earlier_chat", "what did we decide about the sales forecast horizon?", uses("ask_knowledge"),
             setup=worker_setup("_knowledge")),
        Case("table", "what were the total sales in February?", uses("analyze_or_code"), setup=worker_setup("_pizza")),
        Case("script", "write and run a python script that counts the words in a sentence", uses("analyze_or_code")),
        Case("plot", "give me a line chart that shows a parabola", all_of(uses("analyze_or_code"), avoids("draw_diagram"))),
        Case("percent", "what is 18% of 2450?", all_of(uses("calculate"), says("441"))),
        Case("equation", "solve x^2 - 5x + 6 = 0", all_of(avoids("analyze_or_code", "research_web"), says(r"\b2\b", r"\b3\b"))),
        Case("pc_files", "how many files in my C drive has", all_of(avoids("analyze_or_code"), answered)),
    ],
}

# The wording before a change, to measure it against (tool_probe --old).
OLD_STAMP = ("Each user message starts with its local date and time in brackets: use it only when time matters "
             "(dates, \"yesterday\", how long ago); never mention it otherwise.")
NEW_STAMP = ("Each user message starts with the user's current local date and time in brackets: use it when time "
             "matters (the time now, dates, \"yesterday\", how long ago); never mention it otherwise.")


def _old_weather() -> None:
    import dataclasses

    from app.assistant import main
    from app.assistant.registry import TOOLS
    assert NEW_STAMP in main.MAIN, "the stamp sentence changed: update suites.NEW_STAMP"
    main.MAIN = main.MAIN.replace(NEW_STAMP, OLD_STAMP)
    tool = TOOLS["get_weather"]
    TOOLS["get_weather"] = dataclasses.replace(tool, description=tool.description.replace("the local time there",
                                                                                          "local time"))


OLD = {"weather": _old_weather}
