"""Every tool the models can call: an input schema (pydantic -> JSON schema, the model sees the
descriptions) and a handler. A call is validated against its schema before anything runs; a
wrong argument comes back as an invalid_arguments result the model can fix.

The descriptions are the middle layer of the instructions (the role prompts are in prompts.py, what to
do next is in the tools' results): what a tool does, one line per kind, and when to pick it over
another tool - each tool's own rules here and nowhere else.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.tools import (analyze_text, ask, calc, clean_data, compare, diagram, file_ops, files, find_patterns,
                       forecast, image, inspect_data, make_chart, memory, ml, query_data, sandbox, vision, weather, web)
from app.tools import run as run_tool
from app.tools.core import ToolError, ToolResult, ok
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    ws: Workspace
    index: SearchIndex
    conversation: str = "default"
    emit: Callable[[dict], None] = field(default=lambda artifact: None)  # artifacts go to the UI
    # The running helper's trace (what it ran and made), so a Stop can keep it as the call's result.
    trace: Any = None
    # When main's window starts (the first turn it still sees): this chat's turns from before it were cut
    # from view, and recall finds them; None = nothing cut, the whole chat is in view.
    visible_since: str | None = None
    # A helper's question asked in place: shows the card and waits for the user (main.Turn.ask); None when
    # there is no user to ask (tests, benchmarks). call_id: main's call the helper runs under.
    ask: Callable[[dict], dict] | None = None
    call_id: str | None = None
    # Looking at an image (main's view_image, generate_image's self-check): a vision call on the helpers' slot;
    # None when there is no vision. Helpers that see images get them in their conversation instead.
    look: Callable[[bytes, str, str], str] | None = None
    # Pinning a file to the chat (main's files pin/unpin): (key, on) -> the chat's pins now; None outside a chat.
    pin: Callable[[str, bool], list[str]] | None = None


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


TABLE_ID = "the table's file name, e.g. 'sales.csv'"
TIME_UNIT = "the period each point covers, for dates: auto picks one from the span"
TIMEOUT = "seconds the run may take (at most 120)"


@dataclass(frozen=True)
class Tool:
    """One tool, defined once: what the model reads (name, description, input schema), what runs (handler)
    and what the user sees while it runs and after (labels: doing, done; mode_labels: the same per mode, for
    files) - the UI gets the labels from the server (GET /assistant/tools) instead of keeping its own list."""
    name: str
    description: str
    input_model: type[_In]
    handler: Callable[[ToolContext, Any], ToolResult]
    labels: tuple[str, str] = ("Working", "Done")
    mode_labels: dict[str, tuple[str, str]] | None = None

    def spec(self) -> dict:
        schema = _inline_refs(self.input_model.model_json_schema())
        _strip_titles(schema)
        typed_optionals(schema)
        return {"name": self.name, "description": self.description, "input_schema": schema}


# ---------------------------------------------------------------- inputs

class WeatherIn(_In):
    location: str | None = Field(None, description="a place, e.g. 'Pune' or 'Paris, France'; omit for the user's location")
    days: int = Field(0, ge=0, le=7, description="days ahead for a daily forecast: 0 = now only, 1 = today and "
                                                 "tomorrow, 7 = the coming week")


class ImageIn(_In):
    prompt: str = Field(description="a detailed visual description of the image")
    size: Literal["square", "wide", "tall"] = Field("square", description="the picture's shape: wide is 16:9, tall 9:16")


class DiagramIn(_In):
    mermaid: str = Field(description="Mermaid code, first line the diagram type, e.g. 'flowchart TD'")
    title: str | None = Field(None, description="a short title shown above it")


class CalcIn(_In):
    expression: str = Field(description="one expression. Arithmetic: '2450 * 0.18', '(1410 - 1250) / 1250 * 100' (^ "
                                        "powers, n! factorials, sqrt ln log10, sin cos tan in radians - degrees as 35°). "
                                        "Maths, in SymPy's notation (x y z t n are symbols): 'x^3 - x^2 + x - 3 = 0', "
                                        "'diff(2/(1+x^2), x)', 'integrate(x*exp(x), x)', 'Integral(Abs(x-1), (x, 0, 2)).evalf()', "
                                        "'limit(sin(x)/x, x, 0)', 'nsolve(x^3 - 3, x, 1)', 'Matrix([[1, 2], [3, 4]]).det()'")


class AskIn(_In):
    question: str = Field(description="one short question")
    options: list[str] | None = Field(None, description="choices to show as buttons (the user can also type or skip)")
    allow_text: bool = Field(True, description="whether the user may type an answer of their own besides the options")

    @field_validator("options", mode="before")
    @classmethod
    def _lines(cls, value):
        """Options written as one text (a model copying a helper's question): one per line, bullets off."""
        if isinstance(value, str):
            return [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip() for line in value.splitlines()
                    if line.strip()]
        return value


class PlanStep(_In):
    do: str = Field(description="the step, in a few words, e.g. 'find this year's prices'")
    tool: str | None = Field(None, description="the tool or helper that does it, e.g. 'research_web'; omit for a "
                                               "step you do yourself in your answer (summing up, explaining)")


class PlanIn(_In):
    steps: list[PlanStep] = Field(min_length=2, max_length=8, description="2 to 8 steps, in the order they run")



def _read_page(c: ToolContext, a) -> ToolResult:
    """read_webpage - or this chat's earlier read of the page, or its site's earlier refusal (web: the chat
    remembers what its helpers read) - and what was read kept in the workspace's index (files.index_webpage): the
    knowledge helper can find a page read in earlier research - dated, after the user's own files, for 30 days."""
    known = web.remembered_page(c.conversation, a.url, a.find) or web.refused_site(c.conversation, a.url)
    if known is not None:
        return known
    r = web.read_webpage(a.url, a.find)
    web.remember_read(c.conversation, a.url, a.find, r)
    if r.status in ("ok", "partial") and c.index is not None:
        files.index_webpage(c.index, r.result)
    return r

def _files(c: ToolContext, a) -> ToolResult:
    """main's files: pin and unpin here (they belong to the chat), every other mode in file_ops."""
    if a.mode in ("pin", "unpin"):
        if not a.name:
            return ToolResult("invalid_arguments", {"error": {"message": f"{a.mode} needs name: the file's name.",
                                                              "field": "name"}})
        if c.pin is None:
            return ToolResult("failed", {"error": {"message": "Pins belong to a chat; there is none here."}})
        key = c.ws.resolve(a.name, field="name")
        pins = c.pin(key, a.mode == "pin")
        shown = [c.ws.label(k) for k in pins if k in c.ws.ids()]
        said = (f"{c.ws.label(key)} is pinned: the parts of it that match each message are in view every turn until it "
                f"is unpinned." if a.mode == "pin" else f"{c.ws.label(key)} is unpinned.")
        return ok({"pinned": shown, "note": said},
                  {"type": "pins", "pins": [{"id": k, "name": c.ws.label(k)} for k in pins if k in c.ws.ids()]})
    return file_ops.files(c.ws, c.index, a.mode, name=a.name, page=a.page, start=a.start, content=a.content,
                          old_text=a.old_text, new_text=a.new_text, new_name=a.new_name, link=a.link, query=a.query)


def _run(c: ToolContext, a) -> ToolResult:
    """run - and each chart the program wrote shown as a live card, as make_chart's are."""
    from app.tools.chart_image import spec_of
    r = run_tool.run(c.ws, c.index, a.file, a.input, a.timeout)
    for f in (r.artifact or {}).get("files", []):
        if f.get("chart"):
            c.emit({"tool": "run", **spec_of(c.ws, f["id"])})
    return r


def show_plan(steps: list[PlanStep]) -> ToolResult:
    """The plan as a card; the page ticks a step off when its tool's call ends."""
    from .workers import DELEGATES, MAIN_DIRECT  # (the helpers' module imports this one)
    names = [*MAIN_DIRECT, *DELEGATES]
    unknown = sorted({s.tool for s in steps if s.tool} - set(names))
    if unknown:
        return ToolResult("invalid_arguments", {"error": {"message": f"No tool {', '.join(unknown)}: each step's tool is "
                                                                     f"one of {', '.join(n for n in names if n != 'plan')}.",
                                                          "field": "steps"}})
    return ok({"plan": f"{len(steps)} steps, shown to the user"},
              {"type": "plan", "steps": [{"do": " ".join(s.do.split())[:120], "tool": s.tool or None} for s in steps]})


class TaskIn(_In):
    task: str = Field(description="what to do, complete on its own")


class WorkTaskIn(TaskIn):
    files: list[str] | None = Field(None, description="the workspace files the task is about, by name - tables, "
                                                      "pictures, documents, programs - e.g. ['sales.csv', 'stats.py']")


class QuestionIn(_In):
    question: str = Field(description="what to find in the user's files and earlier chats")


class SearchIn(_In):
    query: str = Field(description="what to look for, in words a page would use")
    site: str | None = Field(None, description="only this domain, e.g. 'python.org'")
    recent: Literal["day", "week", "month", "year"] | None = Field(
        None, description="only pages from the last day, week, month or year - for news and 'latest' questions")


FILE_NAME = "the file's name, e.g. 'report.md' or 'charts/sales.png'"
QUERY = ("find: what to look for - words in a file's name or text, what a picture shows, a table's columns or values, "
         "e.g. 'lion', 'refund policy', 'revenue by region'")
START = "read: the line to read from (the result's next says where to go on)"
CONTENT = "write: the file's whole text (Markdown for .md, .docx and .pptx)"
OLD_TEXT = "edit: the passage to change, exactly as it is in the file (spaces and line breaks too)"
NEW_TEXT = "edit: the text to put instead ('' removes the passage)"


class FilesIn(_In):
    mode: Literal["list", "find", "read", "write", "edit", "rename", "delete", "download", "pin", "unpin"] = Field(
        description="what to do: each mode is described with the tool")
    name: str | None = Field(None, description=FILE_NAME + " (all modes but list, find and download)")
    query: str | None = Field(None, description=QUERY)
    page: int = Field(1, ge=1, description="list: which page")
    start: int | None = Field(None, ge=0, description=START)
    content: str | None = Field(None, description=CONTENT)
    old_text: str | None = Field(None, description=OLD_TEXT)
    new_text: str | None = Field(None, description=NEW_TEXT)
    new_name: str | None = Field(None, description="rename: the new name")
    link: str | None = Field(None, description="download: a GitHub file's link, or a data file's link (CSV, Excel)")


class HelperFilesIn(_In):
    mode: Literal["find", "read", "write", "edit"] = Field(description="what to do: each mode is described with the tool")
    name: str | None = Field(None, description=FILE_NAME + " (read, write, edit)")
    query: str | None = Field(None, description=QUERY)
    start: int | None = Field(None, ge=0, description=START)
    content: str | None = Field(None, description=CONTENT)
    old_text: str | None = Field(None, description=OLD_TEXT)
    new_text: str | None = Field(None, description=NEW_TEXT)


class ReaderFilesIn(_In):
    mode: Literal["find", "read"] = Field(description="find: which files match; read: one file's text")
    query: str | None = Field(None, description=QUERY)
    name: str | None = Field(None, description="read: " + FILE_NAME)
    start: int | None = Field(None, ge=0, description=START)


class ReadPageIn(_In):
    url: str = Field(description="the page's address, e.g. from a search result or the task")
    find: str | None = Field(None, description="for a long page: words to look for; the parts about them are shown")


class FilesQueryIn(_In):
    query: str = Field(description="what to look for, in words the document would use")


class DataIn(_In):
    data: str = Field(description=TABLE_ID)


class CleanIn(_In):
    data: str = Field(description=TABLE_ID)
    steps: list[dict] = Field(description="steps in order, each {'op': ..., ...}: parse_numbers{columns}, "
                              "parse_dates{columns, order: auto|dayfirst|monthfirst}, drop_duplicates{columns?}, "
                              "drop_rows{column, values}, "
                              "fill_missing{columns, method: mean|median|mode|zero|value|forward, value?}, "
                              "drop_missing{columns?}, tidy_text{columns}, flag_outliers{columns}, "
                              "remove_outliers{columns}, "
                              "rename_columns{mapping}, drop_columns{columns}")


class SqlIn(_In):
    sql: str = Field(description="one DuckDB SELECT; a table is named by its file name in double quotes, e.g. SELECT "
                     "region, sum(amount) FROM \"sales.csv\" GROUP BY 1. For weekday or month names use dayname(date) / "
                     "monthname(date), not numbers")
    save: bool = Field(False, description="save the full result as a new table (to chart, test or model it)")


class ChartIn(_In):
    data: str = Field(description=TABLE_ID)
    type: Literal["line", "area", "bar", "scatter", "histogram", "box", "heatmap", "pie"] = Field(
        description="the kind of chart: which fits what is in the tool's description")
    x: str = Field(description="line: the date or number column; bar, pie: the categories; scatter: a number; "
                               "histogram: the number; box: the groups (or the number, for one box); heatmap: "
                               "the columns' categories")
    y: str | None = Field(None, description="the number column, combined by agg; omit to count rows")
    by: str | None = Field(None, description="split into groups: one line each (line), stacked (area), grouped bars "
                                             "(bar), one colour each (scatter), one spread each (histogram), or the "
                                             "heatmap's rows")
    agg: Literal["sum", "mean", "median", "count", "min", "max"] = Field(
        "sum", description="how y is combined in each category or period: sum for amounts, mean for rates or prices")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = Field("auto", description=TIME_UNIT)
    top: int = Field(20, ge=1, le=50, description="bar and box: how many categories")
    stacked: bool = Field(False, description="bar with by: stack the groups instead of side by side")
    title: str | None = Field(None, description="the chart's title (omit for one made from the columns)")


class CompareIn(_In):
    data: str = Field(description=TABLE_ID)
    question: Literal["difference", "correlation", "association", "ab"] = Field(
        description="difference: a number across groups; correlation: two numbers; association: two categories; "
                    "ab: an A/B test - 2 variants and their outcome (yes/no or a number)")
    columns: list[str] = Field(min_length=2, max_length=2, description="the two columns; for ab: [variant, outcome]")


class PredictIn(_In):
    data: str = Field(description=TABLE_ID)
    target: str = Field(description="the column to predict (a number, yes/no, or a few classes)")
    features: list[str] | None = Field(None, description="columns to learn from; omit for all usable ones")
    time_column: str | None = Field(None, description="a date column: the model is tested on the latest rows, "
                                                      "learned from the earlier ones (omit: the table's one date "
                                                      "column, if it has one)")
    new_data: str | None = Field(None, description="another table with the same columns to predict for, e.g. "
                                                   "next month's rows")


class ForecastIn(_In):
    data: str = Field(description=TABLE_ID)
    value: str | None = Field(None, description="number column; omit to forecast the row count")
    date: str | None = Field(None, description="the date column's name (not a date); omit for the table's date "
                                               "column. Year + quarter (or month) columns are combined for you")
    by: str | None = Field(None, description="a category column: one forecast per group (the 5 largest)")
    horizon: int | None = Field(None, ge=1, le=60, description="periods ahead")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = Field("auto", description=TIME_UNIT)
    agg: Literal["sum", "mean", "last", "count"] = Field(
        "sum", description="how a period's values combine: sum (sales), mean (a rate), last (a balance), count")


class PatternsIn(_In):
    data: str = Field(description=TABLE_ID)
    kind: Literal["segments", "unusual", "together"] = Field(
        description="segments: groups of similar customers (or rows); unusual: unusual days/periods (or rows); "
                    "together: items in the same order more often than chance")
    group: str | None = Field(None, description="segments: what to group, e.g. 'customer_id' (omit for the table's "
                                                "customer column, or the rows)")
    columns: list[str] | None = Field(None, description="segments: the numbers to describe each one by (omit: "
                                                        "chosen for you); unusual: several numbers to check rows by")
    value: str | None = Field(None, description="unusual: the number to watch per day/period - summed per period for "
                                                "you, so give the table as it is (omit to count rows)")
    by: str | None = Field(None, description="unusual: one series per group (the 5 largest)")
    basket: str | None = Field(None, description="together: the order/visit column (omit for the table's own)")
    item: str | None = Field(None, description="together: the item column (omit for the table's own)")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = Field("auto", description=TIME_UNIT)


class ViewImageIn(_In):
    image: str = Field(description="the image's name, e.g. 'panda.jpg'")
    question: str = Field(description="what to look for, e.g. 'Is the title readable and does it say 2025?' or "
                                      "'Describe this image'")


class TextIn(_In):
    data: str = Field(description=TABLE_ID)
    kind: Literal["themes", "classify", "similar"] = Field(
        description="themes: what the texts are about; classify: sort them into your labels; similar: the rows "
                    "most like a phrase")
    column: str | None = Field(None, description="the text column (omit for the table's free-text column)")
    labels: list[str] | None = Field(None, description="classify: 2-12 labels, e.g. ['delivery', 'price', 'quality']")
    query: str | None = Field(None, description="similar: the phrase, e.g. 'late delivery'")
    count: int | None = Field(None, ge=2, le=12, description="themes: how many (omit to choose)")
    value: str | None = Field(None, description="themes, classify: a number to average per theme, e.g. a rating")


TYPED = "what a person would type when the program asks (input()), one line per answer, e.g. 'w\\nw\\nq'"


class RunIn(_In):
    file: str = Field(description="the Python file to run, by its name, e.g. 'scripts/age_by_class.py'")
    input: str | None = Field(None, description=TYPED)
    timeout: int = Field(30, ge=1, le=120, description=TIMEOUT)


# ---------------------------------------------------------------- the tools

FILE_MODE_LABELS = {"list": ("Listing files", "Listed files"), "find": ("Searching your files", "Searched your files"),
                    "read": ("Reading a file", "Read a file"), "write": ("Writing a file", "Wrote a file"),
                    "edit": ("Changing a file", "Changed a file"), "rename": ("Renaming a file", "Renamed a file"),
                    "delete": ("Asking to delete a file", "Asked to delete a file"),
                    "download": ("Asking to download", "Asked to download"),
                    "pin": ("Pinning a file", "Pinned a file"), "unpin": ("Unpinning a file", "Unpinned a file")}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_weather", "Weather where the user is - found automatically, don't ask them - or in a named place: now "
       "(temperature, UV, air quality, the local time there), and with days a daily forecast (days=1 covers "
       "tomorrow). Use it when the user asks about the weather, rain, heat, air or what to wear or carry outside, "
       "or the time in another place.",
       WeatherIn, lambda c, a: weather.get_weather(a.location, a.days),
       labels=("Checking the weather", "Checked the weather")),
    Tool("generate_image", "Generate one picture - a scene, art or a photo-like image - from a detailed description; the user sees "
       "it. Not for anything that must be exact or labelled (an image model draws labels and numbers wrong): how "
       "something works or is built goes to draw_diagram; a chart of a table's numbers, a plot of a formula or a "
       "simulation to analyze_or_code.",
       ImageIn, lambda c, a: image.generate_image(c.ws, a.prompt, a.size, look=c.look),
       labels=("Creating an image", "Created an image")),
    Tool("draw_diagram", "Draw a diagram from Mermaid code, in the kind that fits: flowchart (steps and decisions), sequence "
       "(messages between parties, in order), class, state (the states something moves through), ER (data and "
       "its relations), gantt (tasks on dates), pie (a few shares), mindmap or timeline. Use it to show how "
       "something works or is built, or when the user asks for a visual explanation. Not for a table's numbers "
       "or a plot of a formula (analyze_or_code charts and plots them).",
       DiagramIn, lambda c, a: diagram.draw_diagram(a.mermaid, a.title),
       labels=("Drawing a diagram", "Drew a diagram")),
    Tool("calculate", "Exact maths: use it for every number you work out - even a sum or an average - instead of "
       "doing it in your head. Arithmetic, and maths: equations (solve, or 'lhs = rhs'; nsolve for a number), "
       "derivatives (diff), integrals (integrate; a definite integral's number: Integral(f, (x, a, b)).evalf()), "
       "limits, sums, matrices. A maths problem is yours: solve it step by step, every number from here - a helper "
       "only for maths that needs a program (a simulation, many cases). Not for a table's numbers (analyze_or_code).",
       CalcIn, lambda c, a: calc.calculate(a.expression),
       labels=("Calculating", "Calculated")),
    Tool("plan", "Show the user your plan before a job of several steps that build on each other (find data, then "
       "analyse it, then make a file): one short line per step, with the tool or helper that does it. The user sees it "
       "as a checklist that ticks off as the tools finish. Not for a single step or a plain answer.",
       PlanIn, lambda c, a: show_plan(a.steps),
       labels=("Planning", "Planned")),
    Tool("ask_user", "Ask the user one question, only when the request could reasonably mean different things "
       "with different results. When one reading can be answered as it stands (e.g. a record, a top or a total), "
       "answer that one and say which reading you took instead. Never ask about preferences that don't change the "
       "answer.",
       AskIn, lambda c, a: ask.ask_user(a.question, a.options, a.allow_text),
       labels=("Asking you", "Asked you")),
    # worker tools
    Tool("web_search", "Search the web: titles, links and short snippets - enough to choose pages, not to answer "
       "from: read the best ones with read_webpage.",
       SearchIn, lambda c, a: web.web_search(a.query, a.site, recent=a.recent,
                                             embedder=getattr(c.index, "embedder", None)),
       labels=("Searching the web", "Searched the web")),
    Tool("read_webpage", "Read the text of one web page (HTML, text, or a PDF, Word or PowerPoint file; a data file "
       "shows its columns and first rows). A YouTube link gives the video's title, channel, length, date and "
       "description (not what is said in it).", ReadPageIn,
       lambda c, a: _read_page(c, a),
       labels=("Reading a page", "Read a page")),
    Tool("files", "The workspace's files: what the user has, and the files you make. Use it when the user asks what "
       "files they have or what is in them, or to open, write, change, rename, delete or bring in one. list: everything, "
       "a page at a time; find: which files match a query - by name and by what is in them (a document's passages, "
       "code, a chart, a table's columns and values, what a picture shows), each with the part that matched - one "
       "call answers 'which' and 'how many'; read: a file's report (what a helper did with it), or its text "
       "from line start on (an image or a table says which tool reads it); write: a new file - text, Markdown, CSV, "
       "JSON, code, and documents: a Word .docx or slides .pptx are made from Markdown (for slides each # heading "
       "starts a slide; a line that is only ![caption](charts/sales.chart.json) puts in that picture or chart) - or the whole "
       "new text of a file that is there (its version before is kept); edit: change one passage - old_text exactly as "
       "in the file, new_text to put instead; rename; delete: one file; download: one file from GitHub, or a data "
       "table (CSV, Excel) from any public link - a repository's or folder's page lists the files it has, to pick "
       "one. agent.md is your memory for this project, always in view: to remember something (\"remember ...\", "
       "\"from now on ...\") edit it - a line under the right heading. pin: keep a file in view in this chat - "
       "the parts of it that match each message come with every turn (the user pins files too); unpin: stop. "
       "Not for running code (analyze_or_code) or "
       "finding where something is on the web (research_web). Delete, "
       "download and changing the user's own file ask the user with a button - call it straight away, don't ask "
       "first in words.",
       FilesIn, _files,
       labels=("Working with files", "Worked with files"), mode_labels=FILE_MODE_LABELS),
    Tool("recall", "Find what was said in earlier conversations - and earlier in this one, when it is out of view.",
       FilesQueryIn,
       lambda c, a: memory.recall(c.index, a.query, exclude_conversation=c.conversation, visible_since=c.visible_since),
       labels=("Searching earlier chats", "Searched earlier chats")),
    Tool("inspect_data", "First look at a table: columns, types, missing values, duplicates, outliers, suggested "
       "cleaning steps and questions only the user can answer.", DataIn,
       lambda c, a: inspect_data.inspect_data(c.ws, a.data),
       labels=("Looking at the table", "Looked at the table")),
    Tool("clean_data", "Apply cleaning steps to a table; saves a new table and a log. Nothing is changed without a step.",
       CleanIn, lambda c, a: clean_data.clean_data(c.ws, a.data, a.steps),
       labels=("Cleaning the table", "Cleaned the table")),
    Tool("query_data", "Exact numbers with one read-only SQL query: totals, rankings, filters, group-bys - and "
       "plain arithmetic, which needs no table: SELECT (1410 - 1250) / 1250 * 100.",
       SqlIn, lambda c, a: query_data.query_data(c.ws, a.sql, a.save),
       labels=("Querying the data", "Queried the data")),
    Tool("make_chart", "Draw a chart of a table, computed from all its rows, kept as a chart file in charts/ (its "
       "name comes back as chart_file: a document or slide takes it as a picture): line (over time; by = one line "
       "per group), area (a line filled; by = stacked groups), bar (by category; by = grouped or stacked), scatter "
       "(two numbers, with a trend line), histogram (how one number spreads; by = one spread per group, e.g. the "
       "ages of survivors and the rest), box (a number's spread per group), heatmap (two categories, e.g. weekday x "
       "hour) or pie (shares, up to 5 slices). It groups and sums (agg) itself: no need to aggregate with "
       "query_data first. The chart the user needs is drawn here, not in a script.",
       ChartIn, lambda c, a: make_chart.make_chart(c.ws, a.data, a.type, a.x, a.y, a.by, a.agg, a.time_unit, a.top,
                                                   a.stacked, a.title),
       labels=("Drawing a chart", "Drew a chart")),
    Tool("compare", "Is a difference, correlation, association or A/B result real, or chance? The test is chosen "
       "from the columns; the result gives the size of the effect with a 95% range.",
       CompareIn, lambda c, a: compare.compare(c.ws, a.data, a.question, a.columns),
       labels=("Testing the numbers", "Tested the numbers")),
    Tool("predict", "Learn a column from the others (who will churn, what a price will be): tested against a "
       "baseline, with the drivers and their direction (associations, not causes) and leak warnings; saves the "
       "table with the predictions (and new_data's) as a new table.", PredictIn,
       lambda c, a: ml.predict(c.ws, a.data, a.target, a.features, a.time_column, a.new_data),
       labels=("Learning to predict", "Learned to predict")),
    Tool("forecast", "A value over time: is it really rising or falling (trend test), its seasonal pattern "
       "(e.g. peak month), the latest period vs a year earlier, and the next periods with an 80% range - "
       "tested against a simple baseline; by = one forecast per group.", ForecastIn,
       lambda c, a: forecast.forecast(c.ws, a.data, a.date, a.value, a.horizon, a.time_unit, a.agg, a.by),
       labels=("Forecasting", "Made a forecast")),
    Tool("find_patterns", "Patterns no single query shows: segments (groups of similar customers or rows, what "
       "sets each apart), unusual (days or periods far from their trend and season, or extreme rows) or together "
       "(items bought in the same order more often than chance). Saves a table of the result.", PatternsIn,
       lambda c, a: find_patterns.find_patterns(c.ws, a.data, a.kind, a.group, a.columns, a.value, a.by, a.basket,
                                                a.item, a.time_unit),
       labels=("Looking for patterns", "Looked for patterns")),
    Tool("analyze_text", "What a text column says, by meaning (local embeddings): themes (groups with their words "
       "and examples - you name them), classify (into the user's labels; good on labels with a clear meaning, about "
       "half right on a business's own categories) or similar (rows most like a phrase). Saves the table with each row's theme or label.", TextIn,
       lambda c, a: analyze_text.analyze_text(c.ws, a.data, a.kind, a.column, a.labels, a.query, a.count, a.value,
                                              embedder=c.index.embedder),
       labels=("Reading the texts", "Read the texts")),
    Tool("view_image", "Look at an image (attached, generated, or drawn by a script) and answer a question about "
       "it: what it shows, whether a detail is there, what its text says. Use it when the user attaches or asks about "
       "an image - you can't see images otherwise: look before you describe one. Rough for exact numbers.",
       ViewImageIn,
       lambda c, a: vision.view_image(c.ws, a.image, a.question, c.look),
       labels=("Looking at the image", "Looked at the image")),
    Tool("run", "Run a Python file of the workspace - write it with files first, change it with files edit, run it "
       "again. It runs in the workspace's folder, so files open by their names: pd.read_csv('sales.csv'), "
       "Image.open('panda.jpg'), open('notes.md'); table('sales.csv') gives a table with its dates and numbers "
       "already read. Files it writes are kept in the workspace (.csv / .parquet become tables, pictures are shown "
       "to you); writing again a file the assistant made changes it (its version before is kept); the user's own "
       "files are never changed - write under a new name. A chart from a program: chart('line', x=[...], y=[...], "
       "title='...') (line, area, bar, scatter, histogram, pie; series=[{'name': ..., 'y': [...]}] for several) - "
       "drawn live like make_chart's; matplotlib only for pictures that aren't charts. A failed run shows the lines "
       "around the error. It runs "
       "on Windows: fonts by file name, e.g. ImageFont.truetype('segoeui.ttf', 96) (arial, georgia, segoeui, times, "
       "verdana ...). No internet: a file from the web must be in the workspace first. "
       "Libraries: " + ", ".join(sandbox.INSTALLED) + ".", RunIn,
       lambda c, a: _run(c, a),
       labels=("Running code", "Ran code")),
]}


# A guard that judges a call (numbers that came from nowhere, a call that already failed, the same call again)
# says why it stopped it and ends with CONFIRMABLE: the model sends the same call with confirm: true, instead of
# rewriting it to get past the guard (measured on the 26B: a demo table refused as invented data was rewritten,
# refused again, and the user stopped the turn). Guards for the user's safety and permission never take it.
CONFIRM = "confirm"
CONFIRMABLE = " If you are sure it is right, make the same call again with confirm: true."


def split_confirm(raw) -> tuple[object, bool]:
    """(the call's input without its confirm, whether it was confirmed)."""
    if not isinstance(raw, dict) or CONFIRM not in raw:
        return raw, False
    said = raw[CONFIRM]
    return {k: v for k, v in raw.items() if k != CONFIRM}, said is True or str(said).strip().lower() == "true"


def execute(ctx: ToolContext, name: str, raw_input, tools: dict[str, Tool] | None = None) -> ToolResult:
    """Validate and run one call (from ``tools``, default every registered tool); artifacts go to the
    UI through ctx.emit. A confirm in the input is the guards' business, not the tool's: it is taken off."""
    raw_input, _ = split_confirm(raw_input)
    offered = tools if tools is not None else TOOLS
    tool = offered.get(name)
    if tool is None:  # the ones there are: an old chat may hold calls to tools since merged or renamed
        return ToolResult("invalid_arguments", {"error": {"message": f"There is no tool '{name}'. Tools: "
                                                                     f"{', '.join(offered)}."}})
    if not isinstance(raw_input, dict):
        return ToolResult("invalid_arguments", {"error": {"message": "The arguments were not valid JSON."}})
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "arguments"
        return ToolResult("invalid_arguments", {"error": {"message": f"{where}: {first['msg']}", "field": where}})
    try:
        _resolve_items(ctx, args)
        result = tool.handler(ctx, args)
    except ToolError as exc:  # raised outside a @tool function too (a helper refusing a file): its message counts
        return ToolResult("invalid_arguments" if exc.invalid else "failed", {"error": exc.error()})
    except Exception as exc:  # noqa: BLE001 - a bug in one tool must not end the whole task
        log.exception("tool %s failed on %s", name, raw_input)
        return ToolResult("failed", {"error": {"message": f"{name} stopped with an internal error "
                                               f"({type(exc).__name__}); try other input or another tool."}})
    if result.artifact:
        ctx.emit({"tool": name, **result.artifact})
    return result


# Inputs that name a workspace item, and the kinds each may be: resolved by one resolver before any tool
# runs, so every tool takes a file's name (any case), a copied listing line or a key alike (Workspace.resolve).
ITEM_FIELDS = {"data": ("table",), "new_data": ("table",), "image": ("image",)}
ITEM_FIELDS_BY_INPUT: dict[type, dict[str, tuple[str, ...]]] = {}


def _resolve_items(ctx: ToolContext, args: _In) -> None:
    """Every file named in a call, as its key (in place); a ToolError the model can act on when it isn't there."""
    fields = {**ITEM_FIELDS, **ITEM_FIELDS_BY_INPUT.get(type(args), {})}
    for name, kinds in fields.items():
        value = getattr(args, name, None)
        if isinstance(value, str) and value.strip():
            setattr(args, name, ctx.ws.resolve(value, kinds, field=name))


def specs(names: list[str]) -> list[dict]:
    return [TOOLS[n].spec() for n in names]


def typed_optionals(schema) -> None:
    """In place: an optional field - pydantic writes 'X or null' as anyOf - becomes X with nullable:
    true. A chat template renders each parameter from its 'type'; Gemma's has no anyOf, so every
    optional field reached the model untyped and it guessed. Measured on ask_user's options:
    written as one text 10/10 before, a proper list 9/10 once typed."""
    if isinstance(schema, dict):
        options = schema.get("anyOf")
        if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
            schema.pop("anyOf")
            schema.update(next(o for o in options if o != {"type": "null"}))
            schema["nullable"] = True
        for value in schema.values():
            typed_optionals(value)
    elif isinstance(schema, list):
        for item in schema:
            typed_optionals(item)


def _inline_refs(schema: dict) -> dict:
    """Nested models written in place of their '$ref's (plan's steps): a chat template renders a parameter from
    its own keys, and has no '$defs' to look one up in."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk({**defs[node["$ref"].rsplit("/", 1)[-1]], **{k: v for k, v in node.items() if k != "$ref"}})
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _strip_titles(schema) -> None:
    """Pydantic's 'title' keys repeat the names and cost tokens."""
    if isinstance(schema, dict):
        schema.pop("title", None)
        for key, value in list(schema.items()):
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    _strip_titles(prop)
            else:
                _strip_titles(value)
    elif isinstance(schema, list):
        for item in schema:
            _strip_titles(item)


# A helper's own version of a tool main also has: the same name and handler, the modes it may use.
HELPER_TOOLS: dict[str, Tool] = {
    "files": Tool("files", "The workspace's files, by name. find: which files match a query - by name and by what "
                  "is in them - each with the part that matched; read: a file's text from "
                  "line start on, with its report (an image or a table says what reads it); write: a new file, or "
                  "the whole new text of one (its version before is kept) - a program is written here, then run "
                  "with run; edit: change one passage - old_text exactly as in the file (line numbers as read may "
                  "stay), new_text to put instead - then run it again. A file the user brought is changed only if "
                  "they allow it: they are asked.", HelperFilesIn,
                  lambda c, a: file_ops.files(c.ws, c.index, a.mode, name=a.name, start=a.start, content=a.content,
                                              old_text=a.old_text, new_text=a.new_text, query=a.query, helper=True),
                  labels=("Working with files", "Worked with files"), mode_labels=FILE_MODE_LABELS),
    # the knowledge helper reads and never writes: files with find and read only
    "files:read": Tool("files", "The user's files. find: which files match a query - by name and by what is in them "
                       "(documents, code, charts, tables, pictures) - each with the passage that matched, best first, "
                       "then pages read in earlier research (dated); read: one file's text from line start on.",
                       ReaderFilesIn,
                       lambda c, a: file_ops.files(c.ws, c.index, a.mode, name=a.name, start=a.start, query=a.query,
                                                   helper=True),
                       labels=("Looking through your files", "Looked through your files"), mode_labels=FILE_MODE_LABELS),
}
