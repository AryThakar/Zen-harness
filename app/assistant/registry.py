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
    prompt: str = Field(description="a detailed visual description of the image, e.g. 'a red fox asleep in fresh "
                                    "snow at dawn, soft light, photo'")
    size: Literal["square", "wide", "tall"] = Field("square", description="the picture's shape: wide is 16:9, tall 9:16")


class DiagramIn(_In):
    mermaid: str = Field(description="Mermaid code, first line the diagram type, e.g. 'flowchart TD'")
    title: str | None = Field(None, description="a short title shown above it")


class CalcIn(_In):
    expression: str = Field(description="one expression, or several separated by commas (up to 20, each worked "
                                        "out). Arithmetic: '2450 * 0.18', '(1410 - 1250) / 1250 * 100' (^ "
                                        "powers, n! factorials, sqrt ln log10, sin cos tan in radians - degrees as 35°). "
                                        "Maths, in SymPy's notation (x y z t n are symbols): 'x^3 - x^2 + x - 3 = 0', "
                                        "'diff(2/(1+x^2), x)', 'integrate(x*exp(x), x)', 'Integral(Abs(x-1), (x, 0, 2)).evalf()', "
                                        "'limit(sin(x)/x, x, 0)', 'nsolve(x^3 - 3, x, 1)', 'Matrix([[1, 2], [3, 4]]).det()'")


class AskIn(_In):
    question: str = Field(description="one short question, e.g. 'Which year should the totals cover?'")
    options: list[str] | None = Field(None, description="choices to show as buttons, e.g. ['2024', '2025'] (the user "
                                                        "can also type or skip)")
    allow_text: bool = Field(True, description="whether the user may type an answer of their own besides the options")

    @field_validator("options", mode="before")
    @classmethod
    def _lines(cls, value):
        """Options written as one text (a model copying a helper's question): one per line, bullets off."""
        if isinstance(value, str):
            return [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip() for line in value.splitlines()
                    if line.strip()]
        return value


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
    return file_ops.files(c.ws, c.index, a.mode, name=a.name, page=a.page, start=a.start, end=a.end,
                          content=a.content, old_text=a.old_text, new_text=a.new_text, new_name=a.new_name,
                          link=a.link, query=a.query, table=a.table)


def _run(c: ToolContext, a) -> ToolResult:
    """run - and each chart the program wrote shown as a live card, as make_chart's are."""
    from app.tools.chart_image import spec_of
    r = run_tool.run(c.ws, c.index, a.file, a.input, a.timeout)
    for f in (r.artifact or {}).get("files", []):
        if f.get("chart"):
            c.emit({"tool": "run", **spec_of(c.ws, f["id"])})
    return r


class TaskIn(_In):
    task: str = Field(description="what the user wants and why, in their words, complete on its own - names, "
                                  "numbers, dates, e.g. "
                                  "'Which month of 2024 had the highest revenue? The user is planning stock.'")


class WorkTaskIn(TaskIn):
    files: list[str] | None = Field(None, description="the workspace files the task starts from, by name - they must "
                                                      "exist: tables, pictures, documents, programs - e.g. ['sales.csv', "
                                                      "'stats.py']. A file the helper is to make is named in the task, "
                                                      "not here")


class QuestionIn(_In):
    question: str = Field(description="what to find in the user's files and earlier chats, e.g. 'What notice "
                                      "period does the flat's lease give?'")


class SearchIn(_In):
    query: str = Field(description="what to look for, in words a page would use, e.g. 'python 3.13 release date'")
    site: str | None = Field(None, description="only this domain, e.g. 'python.org'")
    recent: Literal["day", "week", "month", "year"] | None = Field(
        None, description="only pages from the last day, week, month or year - for news and 'latest' questions")


FILE_NAME = "the file's name, e.g. 'report.md' or 'charts/sales.png'"
# The contract of a page the user uses in the chat (the page side: web/js/files.js, pageOf and htmlFrame). Measured
# 2026-10-17: a cup game laid out at a fixed width had its side cups cut off in a narrow chat, and nothing told the
# model the page is sealed (a CDN script or font would not load).
HTML_PAGE = ("an .html page - a game, a tool, a page to use - runs in the chat: write what is on the page (its "
             "HTML, <style> and <script>, all inline); the app adds the head, a base style and Zen's colours as CSS "
             "variables (--zen-bg, --zen-ink, --zen-muted, --zen-line, --zen-accent, --zen-on-accent, --zen-green, "
             "--zen-blue, --zen-red, --zen-orange, --zen-yellow, --zen-purple), lays it out at least 720 px wide and "
             "scales it to fit; it runs as in a browser (canvas, WebGL, WebGPU, keys, localStorage kept for the page), "
             "but nothing loads from the internet (no CDN, web fonts or linked images)")
QUERY = ("find: what to look for - words in a file's name or text, what a picture shows, a table's columns or values, "
         "e.g. 'lion', 'refund policy', 'revenue by region'")
START_READ = "read: the line to read from (the result's next says where to go on)"
START = START_READ + "; edit: the first line to replace, as numbered in the read"
END = "edit: the last line to replace (with start: lines start to end, as numbered in the read, become new_text)"
CONTENT = "write: the file's whole text (Markdown for .md, .docx and .pptx)"
OLD_TEXT = ("edit: the passage to change, exactly as it is in the file (spaces and line breaks too) - or give start and "
            "end instead")
NEW_TEXT = "edit: the text to put instead, with its indentation ('' removes the passage)"


class FilesIn(_In):
    mode: Literal["list", "find", "read", "write", "edit", "rename", "delete", "download", "pin", "unpin"] = Field(
        description="what to do: each mode is described with the tool")
    name: str | None = Field(None, description=FILE_NAME + " (all modes but list, find and download)")
    query: str | None = Field(None, description=QUERY)
    page: int = Field(1, ge=1, description="list: which page")
    start: int | None = Field(None, ge=0, description=START)
    end: int | None = Field(None, ge=1, description=END)
    content: str | None = Field(None, description=CONTENT)
    old_text: str | None = Field(None, description=OLD_TEXT)
    new_text: str | None = Field(None, description=NEW_TEXT)
    new_name: str | None = Field(None, description="rename: the new name")
    link: str | None = Field(None, description="download: a GitHub file's link, a data file's link (CSV, Excel), or "
                                               "a page whose table holds the data")
    table: int | None = Field(None, ge=1, description="download, a page with several tables: which one, e.g. 2 (the "
                                                      "result says how many there are); omit for the largest")


class HelperFilesIn(_In):
    mode: Literal["find", "read", "write", "edit"] = Field(description="what to do: each mode is described with the tool")
    name: str | None = Field(None, description=FILE_NAME + " (read, write, edit)")
    query: str | None = Field(None, description=QUERY)
    start: int | None = Field(None, ge=0, description=START)
    end: int | None = Field(None, ge=1, description=END)
    content: str | None = Field(None, description=CONTENT)
    old_text: str | None = Field(None, description=OLD_TEXT)
    new_text: str | None = Field(None, description=NEW_TEXT)


class ReaderFilesIn(_In):
    mode: Literal["find", "read"] = Field(description="find: which files match; read: one file's text")
    query: str | None = Field(None, description=QUERY)
    name: str | None = Field(None, description="read: " + FILE_NAME)
    start: int | None = Field(None, ge=0, description=START_READ)  # it reads and never edits


class ReadPageIn(_In):
    url: str = Field(description="the page's address, e.g. from a search result or the task")
    find: str | None = Field(None, description="for a long page: words to look for, e.g. 'pricing'; the parts "
                                               "about them are shown")


class FilesQueryIn(_In):
    query: str = Field(description="what to look for, in words the chat would have used, e.g. 'forecast horizon'")


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
    also_y: list[str] | None = Field(None, description="line, area, bar: more number columns drawn beside y, one line "
                                                       "or bar group each, e.g. ['min_c'] with y='max_c'")
    by: str | None = Field(None, description="split into groups: one line each (line), stacked (area), grouped bars "
                                             "(bar), one colour each (scatter), one spread each (histogram), or the "
                                             "heatmap's rows")
    agg: Literal["sum", "mean", "median", "count", "min", "max"] | None = Field(
        None, description="how y is combined in each category or period: sum for amounts, mean for rates or prices "
                          "(omit: mean when y is named as a level - a price, close, rate, score - else sum)")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = Field("auto", description=TIME_UNIT)
    top: int = Field(20, ge=1, le=50, description="bar and box: how many categories")
    stacked: bool = Field(False, description="bar with by: stack the groups instead of side by side")
    title: str | None = Field(None, description="the chart's title (omit for one made from the columns)")


class CompareIn(_In):
    data: str = Field(description=TABLE_ID)
    question: Literal["difference", "correlation", "association", "ab"] = Field(
        description="difference: a number across groups; correlation: two numbers; association: two categories; "
                    "ab: an A/B test - 2 variants and their outcome (yes/no or a number)")
    columns: list[str] = Field(min_length=2, max_length=2, description="the two columns, e.g. ['region', 'amount']; "
                                                                       "for ab: [variant, outcome]")


class PredictIn(_In):
    data: str = Field(description=TABLE_ID)
    target: str = Field(description="the column to predict (a number, yes/no, or a few classes), e.g. 'churned'")
    features: list[str] | None = Field(None, description="columns to learn from; omit for all usable ones")
    time_column: str | None = Field(None, description="a date column: the model is tested on the latest rows, "
                                                      "learned from the earlier ones (omit: the table's one date "
                                                      "column, if it has one)")
    new_data: str | None = Field(None, description="another table with the same columns to predict for, e.g. "
                                                   "next month's rows")


class ForecastIn(_In):
    data: str = Field(description=TABLE_ID)
    value: str | None = Field(None, description="the number column, e.g. 'amount'; omit to forecast the row count")
    date: str | None = Field(None, description="the date column's name (not a date); omit for the table's date "
                                               "column. Year + quarter (or month) columns are combined for you")
    by: str | None = Field(None, description="a category column: one forecast per group (the 5 largest)")
    horizon: int | None = Field(None, ge=1, le=60, description="periods ahead")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = Field("auto", description=TIME_UNIT)
    agg: Literal["sum", "mean", "last", "count"] | None = Field(
        None, description="how a period's values combine: sum (sales), mean (a rate, a price), last (a balance), count "
                          "(omit: mean when the value is named as a level - a price, close, rate, score - else sum)")


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
    file: str = Field(description="the Python file to run, by its name, e.g. 'work/age_by_class.py'")
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
    # Each description in one order (docs/history/REBUILD_PLAN.md, section 3): what it does, in the words a user or a task
    # would use; what it returns; when to use it; when not, and which tool instead.
    Tool("get_weather", "Weather and air quality where the user is - found automatically, don't ask them - or in a "
       "named place. Returns the weather now (temperature, feels-like, rain, wind, UV, air quality) and the local "
       "time there; with days, a daily forecast too (days=1 covers tomorrow). Use it for the weather, rain, heat, the "
       "air, what to wear or carry outside, and the time in another place. Not for past weather or a place's climate "
       "(research_web).",
       WeatherIn, lambda c, a: weather.get_weather(a.location, a.days),
       labels=("Checking the weather", "Checked the weather")),
    Tool("generate_image", "Generate one new picture - a scene, art, a photo-like image - from a detailed "
       "description. Returns the picture, shown to the user. Use it when the user asks for a picture to be made. Not "
       "for anything that must be exact or labelled (an image model draws text and numbers wrong): how something "
       "works or is built goes to draw_diagram; a chart of numbers, a plot of a formula or a change to a picture the "
       "user has to analyze_or_code.",
       ImageIn, lambda c, a: image.generate_image(c.ws, a.prompt, a.size, look=c.look),
       labels=("Creating an image", "Created an image")),
    Tool("draw_diagram", "Draw a diagram from Mermaid code, in the kind that fits: flowchart (steps and decisions), "
       "sequence (messages between parties, in order), class, state (the states something moves through), ER (data "
       "and its relations), gantt (tasks on dates), pie (a few shares), mindmap or timeline. Returns the drawn "
       "diagram, shown to the user. Use it to show how something works or is built, or when the user asks for a "
       "visual explanation. Not for a table's numbers or a plot of a formula (analyze_or_code), or a picture of a "
       "scene (generate_image).",
       DiagramIn, lambda c, a: diagram.draw_diagram(a.mermaid, a.title),
       labels=("Drawing a diagram", "Drew a diagram")),
    Tool("calculate", "Exact maths with SymPy: arithmetic and percentages, equations (solve, or 'lhs = rhs'; nsolve "
       "for a number), derivatives (diff), integrals (integrate; a definite integral's number: Integral(f, (x, a, "
       "b)).evalf()), limits, sums, matrices. Returns the exact result (and its decimal value). Use it for every "
       "number you work out - even a sum or an average - instead of doing it in your head; a maths problem is yours: "
       "solve it step by step, every number from here. Not for a table's numbers, or maths that needs a program - a "
       "simulation, many cases (analyze_or_code).",
       CalcIn, lambda c, a: calc.calculate(a.expression),
       labels=("Calculating", "Calculated")),
    Tool("ask_user", "Ask the user one question, with choices as buttons, and wait. Returns their answer (or that "
       "they skipped it). Use it only when the request could reasonably mean different things with different "
       "results. Not when one reading can be answered as it stands (e.g. a record, a top or a total): answer that one "
       "and say which reading you took. Never ask about preferences that don't change the answer.",
       AskIn, lambda c, a: ask.ask_user(a.question, a.options, a.allow_text),
       labels=("Asking you", "Asked you")),
    # worker tools
    Tool("web_search", "Search the web. Returns titles, links and short snippets - enough to choose pages, not to "
       "answer from. Use it to find the pages to read, then read the best ones with read_webpage. Not for a page whose "
       "link you have (read_webpage).",
       SearchIn, lambda c, a: web.web_search(a.query, a.site, recent=a.recent,
                                             embedder=getattr(c.index, "embedder", None)),
       labels=("Searching the web", "Searched the web")),
    Tool("read_webpage", "Read one web page: HTML, text, or a PDF, Word or PowerPoint file; a data file shows its "
       "columns and first rows; a YouTube link gives the video's title, channel, length, date and description (not "
       "what is said in it). Returns the page's text - for a long page, the parts about find - with its date when it "
       "has one and the number to cite it by. Use it on the best search results and on a link the task gives. A site "
       "that refuses reading (a bot check) says so: reading it again gets the same answer.", ReadPageIn,
       lambda c, a: _read_page(c, a),
       labels=("Reading a page", "Read a page")),
    Tool("files", "The workspace's files - the user's and the ones you make - by name. Modes: list: everything, a "
       "page at a time; find: which files match a query - by name and by what is in them (a document's passages, "
       "code, a chart, a table's columns and values, what a picture shows), each with the part that matched - one "
       "call answers 'which' and 'how many'; read: a file's text from line start on, with its report (what a helper "
       "did with it) - an image or a table says which tool reads it; write: a new file, or the whole new text of one "
       "(its version before is kept) - text, Markdown, CSV, JSON, code, and documents: a Word .docx or slides .pptx "
       "are made from Markdown (for slides each # heading starts a slide; a line that is only "
       "![caption](charts/sales.chart.json) puts in that picture or chart), and " + HTML_PAGE + "; edit: change one "
       "passage - lines start to end as the read numbered them, or old_text exactly as in the file - to new_text; "
       "rename; delete: one file; download: one "
       "file from GitHub, or "
       "a data table from any public link - a CSV or Excel file, or a page's own table (a price history, a results "
       "table) - a repository's or folder's page lists its files, to pick one; pin: keep a file in view in this chat - the parts of it that match each message come with every turn "
       "(the user pins files too); unpin: stop. agent.md is your memory for this project, always in view: to "
       "remember something (\"remember ...\", \"from now on ...\") write it: content = what it holds plus the new "
       "line. Returns what was asked: the list, the matches, the text, or what was written. Use it when the user asks "
       "what files they have or what is in them, for a quick lookup (which files, one passage), and to make, change, "
       "rename, delete, bring in or pin one. Not for reading many or long documents (ask_knowledge), running code or "
       "a table's numbers (analyze_or_code), or the web (research_web). Delete, download and changing the user's own "
       "file ask the user with a button - call it straight away, don't ask first in words.",
       FilesIn, _files,
       labels=("Working with files", "Worked with files"), mode_labels=FILE_MODE_LABELS),
    Tool("recall", "Find what was said in earlier conversations - and earlier in this one, when it is out of view. "
       "Returns the passages that match, each with its chat. Use it for what was said, asked or decided before. Not "
       "for the user's files (files, mode='find').",
       FilesQueryIn,
       lambda c, a: memory.recall(c.index, a.query, exclude_conversation=c.conversation, visible_since=c.visible_since),
       labels=("Searching earlier chats", "Searched earlier chats")),
    Tool("inspect_data", "First look at a table. Returns its columns with their types and roles (the date, order, "
       "item, customer and money columns), missing values, duplicates, outliers, sample rows, suggested cleaning "
       "steps and questions only the user can answer. Use it first on every table, before anything is computed on "
       "it. Not for a number from it (query_data).", DataIn,
       lambda c, a: inspect_data.inspect_data(c.ws, a.data),
       labels=("Looking at the table", "Looked at the table")),
    Tool("clean_data", "Apply cleaning steps to a table, in order: parse numbers and dates, drop duplicates or rows, "
       "fill or drop missing values, tidy text, flag or remove outliers, rename or drop columns. Returns the new "
       "table's name - use it from then on - with the rows before and after and a log of each step; the table it "
       "came from stays as it was. Use it with the steps inspect_data suggests, all in one call. Nothing is changed "
       "without a step.",
       CleanIn, lambda c, a: clean_data.clean_data(c.ws, a.data, a.steps),
       labels=("Cleaning the table", "Cleaned the table")),
    Tool("query_data", "Exact numbers with one read-only SQL query (DuckDB): totals, counts, averages, rankings, "
       "shares, filters, group-bys - and plain arithmetic, which needs no table: SELECT (1410 - 1250) / 1250 * 100. "
       "Returns the result's columns and rows (the first ones, with the total count); with save, the full result as "
       "a new table. Use it for any number from a table, and to make a table another tool needs (a target column, a "
       "subset). Not for a chart (make_chart) or a test, forecast or model (compare, forecast, predict).",
       SqlIn, lambda c, a: query_data.query_data(c.ws, a.sql, a.save),
       labels=("Querying the data", "Queried the data")),
    Tool("make_chart", "Draw a chart of a table, computed from all its rows: line (over time; by = one line per "
       "group), area (a line filled; by = stacked groups), bar (by category; by = grouped or stacked), scatter (two "
       "numbers, with a trend line), histogram (how one number spreads; by = one spread per group, e.g. the ages of "
       "survivors and the rest), box (a number's spread per group), heatmap (two categories, e.g. weekday x hour) or "
       "pie (shares, up to 5 slices). It groups and sums (agg) itself: no need to aggregate with query_data first. "
       "Returns the numbers drawn and chart_file, the chart file kept in charts/ - a document or slide takes it as a "
       "picture. Use it for every chart the user needs. Not for a chart only a program can compute (a script's "
       "chart()).",
       ChartIn, lambda c, a: make_chart.make_chart(c.ws, a.data, a.type, a.x, a.y, a.by, a.agg, a.time_unit, a.top,
                                                   a.stacked, a.title, a.also_y),
       labels=("Drawing a chart", "Drew a chart")),
    Tool("compare", "Statistical test: is a difference, correlation, association or A/B result real, or chance? The "
       "test is chosen from the columns - Mann-Whitney or Kruskal-Wallis for a number across groups, Spearman for two "
       "numbers, chi-square or Fisher for two categories, a two-proportion z-test or Welch's t-test for an A/B test. "
       "Returns the test, the p-value, the size of the effect with a 95% range and a plain verdict. Use it whenever "
       "the question is whether a gap or a link is real (significant). Not for the size of a gap alone (query_data).",
       CompareIn, lambda c, a: compare.compare(c.ws, a.data, a.question, a.columns),
       labels=("Testing the numbers", "Tested the numbers")),
    Tool("predict", "Train and test a machine-learning model that learns a column from the others (who will churn, "
       "what a price will be): classification or regression; logistic or ridge regression against gradient boosting, "
       "the better kept; tested on the latest rows (with a date) or by 5-fold cross-validation, always against a "
       "baseline. Returns the score and verdict, the drivers - permutation importance and their direction "
       "(associations, not causes) - leak warnings, and the table with the predictions (and new_data's) saved as a "
       "new table. Use it for what predicts or drives a column and for predictions for rows. Not for a value's "
       "future over time (forecast).", PredictIn,
       lambda c, a: ml.predict(c.ws, a.data, a.target, a.features, a.time_column, a.new_data),
       labels=("Learning to predict", "Learned to predict")),
    Tool("forecast", "Time series: a value over time - is it really rising or falling (Mann-Kendall trend test), its "
       "seasonal pattern (e.g. the peak month), the latest period against a year earlier, and the next periods with "
       "an 80% range, from exponential smoothing or Theta tested against a seasonal baseline; by = one forecast per "
       "group. Returns those, with how reliable the forecast is, and a chart file. Use it for trends, seasons and "
       "what comes next. Not for one period's total (query_data).", ForecastIn,
       lambda c, a: forecast.forecast(c.ws, a.data, a.date, a.value, a.horizon, a.time_unit, a.agg, a.by),
       labels=("Forecasting", "Made a forecast")),
    Tool("find_patterns", "Patterns no single query shows: segments (clustering - k-means groups of similar "
       "customers or rows, what sets each apart), unusual (anomalies - days or periods far from their trend and "
       "season, or extreme rows) or together (items bought in the same order more often than chance: lift, with a "
       "test). Returns each group, anomaly or pair with how clear it is, and saves a table of the result. Not for a "
       "ranking or a total (query_data).", PatternsIn,
       lambda c, a: find_patterns.find_patterns(c.ws, a.data, a.kind, a.group, a.columns, a.value, a.by, a.basket,
                                                a.item, a.time_unit),
       labels=("Looking for patterns", "Looked for patterns")),
    Tool("analyze_text", "What a text column says, by meaning (local embeddings): themes (groups with their words "
       "and examples - you name them), classify (into the user's labels; good on labels with a clear meaning, about "
       "half right on a business's own categories) or similar (rows most like a phrase). Returns the themes, labels "
       "or matches with their shares and examples, and saves the table with each row's theme or label. Not for "
       "counting exact words or values (query_data).", TextIn,
       lambda c, a: analyze_text.analyze_text(c.ws, a.data, a.kind, a.column, a.labels, a.query, a.count, a.value,
                                              embedder=c.index.embedder),
       labels=("Reading the texts", "Read the texts")),
    Tool("view_image", "Look at an image in the workspace (attached, generated, or drawn by a script) and answer a "
       "question about it: what it shows, whether a detail is there, what its text says. Returns the answer. Use it "
       "when the user attaches or asks about an image - you can't see images otherwise: look before you describe "
       "one. Rough for exact numbers.",
       ViewImageIn,
       lambda c, a: vision.view_image(c.ws, a.image, a.question, c.look),
       labels=("Looking at the image", "Looked at the image")),
    Tool("run", "Run a Python file of the workspace - write it with files first, change it with files edit, run it "
       "again. Returns what it printed, the files it wrote, and for a failed run the lines around the error. Use it "
       "for programs, simulations, conversions, picture edits and what no data tool does. The environment: it runs "
       "in the workspace's folder, so files open by their names: pd.read_csv('sales.csv'), Image.open('panda.jpg'), "
       "open('notes.md'); table('sales.csv') gives a table with its dates and numbers already read. Files it writes "
       "are kept in the workspace (.csv / .parquet become tables, pictures are shown to you); writing again a file "
       "the assistant made changes it (its version before is kept); the user's own files are never changed - write "
       "under a new name. A chart from a program: chart('line', x=[...], y=[...], title='...') (line, area, bar, "
       "scatter, histogram, pie; series=[{'name': ..., 'y': [...]}] for several) - drawn live like make_chart's; "
       "matplotlib only for pictures that aren't charts. Windows: fonts by file name, e.g. "
       "ImageFont.truetype('segoeui.ttf', 96) (arial, georgia, segoeui, times, verdana ...). No internet: a file "
       "from the web must be in the workspace first. Libraries: " + ", ".join(sandbox.INSTALLED) + ".", RunIn,
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
    """Nested models written in place of their '$ref's: a chat template renders a parameter from
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
    "files": Tool("files", "The workspace's files, by name. Modes: find: which files match a query - by name and "
                  "by what is in them - each with the part that matched; read: a file's text from line start on, "
                  "with its report (an image or a table says what reads it); write: a new file, or the whole new "
                  "text of one (its version before is kept) - a program is written here, then run with run, and "
                  + HTML_PAGE + "; edit: "
                  "change one passage - lines start to end as the read numbered them (the result shows them after), "
                  "or old_text exactly as in the file - to new_text, then run it again. Returns the matches, the "
                  "text, or what was written. Not for a "
                  "table's numbers (the data tools). A file the user brought is changed only if they allow it: they "
                  "are asked.", HelperFilesIn,
                  lambda c, a: file_ops.files(c.ws, c.index, a.mode, name=a.name, start=a.start, end=a.end,
                                              content=a.content, old_text=a.old_text, new_text=a.new_text,
                                              query=a.query, helper=True),
                  labels=("Working with files", "Worked with files"), mode_labels=FILE_MODE_LABELS),
    # the knowledge helper reads and never writes: files with find and read only
    "files:read": Tool("files", "The user's files. Modes: find: which files match a query - by name and by what is "
                       "in them (documents, code, charts, tables, pictures) - each with the passage that matched, best "
                       "first, then pages read in earlier research (dated); read: one file's text from line start on. "
                       "Find first; read a file when the task wants all of it. Not for earlier chats (recall).",
                       ReaderFilesIn,
                       lambda c, a: file_ops.files(c.ws, c.index, a.mode, name=a.name, start=a.start, query=a.query,
                                                   helper=True),
                       labels=("Looking through your files", "Looked through your files"), mode_labels=FILE_MODE_LABELS),
}
