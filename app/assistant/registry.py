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
                       forecast, image, inspect_data, make_chart, memory, ml, notes, query_data, read_file,
                       sandbox, scratch, vision, weather, web)
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
    scratch: scratch.Scratch = field(default_factory=lambda: scratch.Scratch())  # a helper's file, one task long
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


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    size: Literal["square", "wide", "tall"] = "square"


class DiagramIn(_In):
    mermaid: str = Field(description="Mermaid code, first line the diagram type, e.g. 'flowchart TD'")
    title: str | None = None


class CalcIn(_In):
    expression: str = Field(description="arithmetic, e.g. '2450 * 0.18' or '(1410 - 1250) / 1250 * 100'; ^ for "
                                        "powers, n! for factorials, sqrt exp ln log2 log10, sin cos tan asin acos atan "
                                        "(radians: write degrees as 35°), round abs min max, pi and e")


class NotesIn(_In):
    mode: Literal["save", "list", "delete"]
    title: str | None = Field(None, description="save: a short title (saving again with the same title updates "
                                                "the note); delete: the note's title or id")
    content: str | None = Field(None, description="save: a few lines; it is read later on its own, so say 'the user' "
                                                  "and 'the assistant', not I or you")


class AskIn(_In):
    question: str = Field(description="one short question")
    options: list[str] | None = Field(None, description="choices to show as buttons (the user can also type or skip)")
    allow_text: bool = True

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
    tool: str = Field(description="the tool or helper that does it, e.g. 'research_web'")


class PlanIn(_In):
    steps: list[PlanStep] = Field(min_length=2, max_length=8)



def _read_page(c: ToolContext, a) -> ToolResult:
    """read_webpage, and what was read kept in the workspace's index (files.index_webpage): the knowledge
    helper can find a page read in earlier research - dated, after the user's own files, for 30 days."""
    r = web.read_webpage(a.url, a.find)
    if r.status in ("ok", "partial") and c.index is not None:
        files.index_webpage(c.index, r.result)
    return r

def show_plan(steps: list[PlanStep]) -> ToolResult:
    """The plan as a card; the page ticks a step off when its tool's call ends."""
    from .workers import DELEGATES, MAIN_DIRECT  # (the helpers' module imports this one)
    names = [*MAIN_DIRECT, *DELEGATES]
    unknown = sorted({s.tool for s in steps} - set(names))
    if unknown:
        return ToolResult("invalid_arguments", {"error": {"message": f"No tool {', '.join(unknown)}: each step's tool is "
                                                                     f"one of {', '.join(n for n in names if n != 'plan')}.",
                                                          "field": "steps"}})
    return ok({"plan": f"{len(steps)} steps, shown to the user"},
              {"type": "plan", "steps": [{"do": " ".join(s.do.split())[:120], "tool": s.tool} for s in steps]})


class TaskIn(_In):
    task: str = Field(description="what to do, complete on its own")


class CodeTaskIn(TaskIn):
    file: str | None = Field(None, description="a file to change, e.g. 'parabola.py' (omit for a new file)")


class DataTaskIn(TaskIn):
    data: str | None = Field(None, description="the table id to work on, e.g. 'data_1', if the task is about one table")


class QuestionIn(_In):
    question: str = Field(description="what to find in the user's documents, notes and earlier chats")


class SearchIn(_In):
    query: str = Field(description="what to look for, in words a page would use")
    site: str | None = Field(None, description="only this domain, e.g. 'python.org'")
    recent: Literal["day", "week", "month", "year"] | None = Field(
        None, description="only pages from the last day, week, month or year - for news and 'latest' questions")


class FilesIn(_In):
    mode: Literal["list", "find", "read", "write", "update", "rename", "delete", "download"]
    name: str | None = Field(None, description="the file's name or id, e.g. 'report.md' (all modes but list and "
                                               "download)")
    page: int = Field(1, ge=1, description="list: which page")
    start: int | None = Field(None, ge=0, description="read: the line to read from (the result's next says where "
                                                      "to go on); update: the first line to change")
    end: int | None = Field(None, ge=1, description="update: the last line to change")
    content: str | None = Field(None, description="write, update: the text (Markdown for .md and .docx)")
    new_name: str | None = Field(None, description="rename: the new name")
    link: str | None = Field(None, description="download: a GitHub file's link, or a data file's link (CSV, Excel)")


class ReadPageIn(_In):
    url: str = Field(description="the page's address, e.g. from a search result or the task")
    find: str | None = Field(None, description="for a long page: words to look for; the parts about them are shown")


class FilesQueryIn(_In):
    query: str = Field(description="what to look for, in words the document would use")


class ReadFileIn(_In):
    file: str = Field(description="a file or note: its id or name, e.g. 'file_1'")
    start: int = Field(1, ge=0, description="the line to read from; the result's next says where to go on")


class DataIn(_In):
    data: str = Field(description="table id, e.g. 'data_1'")


class CleanIn(_In):
    data: str
    steps: list[dict] = Field(description="steps in order, each {'op': ..., ...}: parse_numbers{columns}, "
                              "parse_dates{columns, order: auto|dayfirst|monthfirst}, drop_duplicates{columns?}, "
                              "drop_rows{column, values}, "
                              "fill_missing{columns, method: mean|median|mode|zero|value|forward, value?}, "
                              "drop_missing{columns?}, tidy_text{columns}, flag_outliers{columns}, "
                              "remove_outliers{columns}, "
                              "rename_columns{mapping}, drop_columns{columns}")


class SqlIn(_In):
    sql: str = Field(description="one DuckDB SELECT; tables are named by id, e.g. SELECT region, sum(amount) FROM "
                     "data_2 GROUP BY 1. For weekday or month names use dayname(date) / monthname(date), not numbers")
    save: bool = Field(False, description="save the full result as a new table (to chart, test or model it)")


class ChartIn(_In):
    data: str
    type: Literal["line", "bar", "scatter", "histogram", "box", "heatmap", "pie"]
    x: str = Field(description="line: the date or number column; bar, pie: the categories; scatter: a number; "
                               "histogram: the number; box: the groups (or the number, for one box); heatmap: "
                               "the columns' categories")
    y: str | None = Field(None, description="the number column, combined by agg; omit to count rows")
    by: str | None = Field(None, description="split into groups: one line each (line), grouped bars (bar), one "
                                             "colour each (scatter), or the heatmap's rows")
    agg: Literal["sum", "mean", "median", "count", "min", "max"] = "sum"
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto"
    top: int = Field(20, ge=1, le=50, description="bar and box: how many categories")
    stacked: bool = Field(False, description="bar with by: stack the groups instead of side by side")
    title: str | None = None


class CompareIn(_In):
    data: str
    question: Literal["difference", "correlation", "association", "ab"] = Field(
        description="difference: a number across groups; correlation: two numbers; association: two categories; "
                    "ab: an A/B test - 2 variants and their outcome (yes/no or a number)")
    columns: list[str] = Field(min_length=2, max_length=2, description="the two columns; for ab: [variant, outcome]")


class PredictIn(_In):
    data: str
    target: str = Field(description="the column to predict (a number, yes/no, or a few classes)")
    features: list[str] | None = Field(None, description="columns to learn from; omit for all usable ones")
    time_column: str | None = None
    new_data: str | None = Field(None, description="another table with the same columns to predict for, e.g. "
                                                   "next month's rows")


class ForecastIn(_In):
    data: str
    value: str | None = Field(None, description="number column; omit to forecast the row count")
    date: str | None = Field(None, description="the date column's name (not a date); omit for the table's date "
                                               "column. Year + quarter (or month) columns are combined for you")
    by: str | None = Field(None, description="a category column: one forecast per group (the 5 largest)")
    horizon: int | None = Field(None, ge=1, le=60, description="periods ahead")
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto"
    agg: Literal["sum", "mean", "last", "count"] = "sum"


class PatternsIn(_In):
    data: str
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
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto"


class ViewImageIn(_In):
    image: str = Field(description="the image's id or name, e.g. 'image_3'")
    question: str = Field(description="what to look for, e.g. 'Is the title readable and does it say 2025?' or "
                                      "'Describe this image'")


class TextIn(_In):
    data: str
    kind: Literal["themes", "classify", "similar"] = Field(
        description="themes: what the texts are about; classify: sort them into your labels; similar: the rows "
                    "most like a phrase")
    column: str | None = Field(None, description="the text column (omit for the table's free-text column)")
    labels: list[str] | None = Field(None, description="classify: 2-12 labels, e.g. ['delivery', 'price', 'quality']")
    query: str | None = Field(None, description="similar: the phrase, e.g. 'late delivery'")
    count: int | None = Field(None, ge=2, le=12, description="themes: how many (omit to choose)")
    value: str | None = Field(None, description="themes, classify: a number to average per theme, e.g. a rating")


TYPED = "what a person would type when the program asks (input()), one line per answer, e.g. 'w\\nw\\nq'"


class RunCodeIn(_In):
    code: str = Field(description="the whole Python script; print what you need")
    timeout: int = Field(30, ge=1, le=120)
    input: str | None = Field(None, description=TYPED)


class FixCodeIn(_In):
    from_line: int = Field(ge=1, description="the first line to change")
    to_line: int = Field(ge=0, description="the last line to change (from_line - 1 inserts before from_line)")
    new_code: str = Field(description="the lines that replace them (may be more or fewer lines)")
    timeout: int = Field(30, ge=1, le=120)
    input: str | None = Field(None, description="typed input as for run_code; omit to type what the last run typed")


# ---------------------------------------------------------------- the tools

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_weather", "Weather where the user is - found automatically, don't ask them - or in a named place: now "
       "(temperature, UV, air quality, the local time there), and with days a daily forecast (days=1 covers "
       "tomorrow). Use it when the user asks about the weather, rain, heat, air or what to wear or carry outside, "
       "or the time in another place.",
       WeatherIn, lambda c, a: weather.get_weather(a.location, a.days),
       labels=("Checking the weather", "Checked the weather")),
    Tool("generate_image", "Generate one picture - a scene, art, a photo-like image - from a detailed description; "
       "the user sees it. Use it when the user asks for a picture, an illustration or art. Not for technical "
       "drawings (circuits, charts, labelled parts): an image model draws them "
       "wrong, with unreadable labels - draw_diagram shows how something works.",
       ImageIn, lambda c, a: image.generate_image(c.ws, a.prompt, a.size, look=c.look),
       labels=("Creating an image", "Created an image")),
    Tool("draw_diagram", "Draw a flowchart, sequence, class, state, ER, gantt, pie, mindmap or timeline diagram "
       "from Mermaid code. Use it when the user wants to see how something works or is built - steps, logic, states, "
       "structure, a plan over time - or asks for a visual explanation.",
       DiagramIn, lambda c, a: diagram.draw_diagram(a.mermaid, a.title),
       labels=("Drawing a diagram", "Drew a diagram")),
    Tool("calculate", "Exact arithmetic: use it for every number you work out - even a sum or an average - "
       "instead of doing it in your head; one call can hold a whole expression. Maths problems: solve them yourself, "
       "step by step, and check the numbers here.",
       CalcIn, lambda c, a: calc.calculate(a.expression),
       labels=("Calculating", "Calculated")),
    Tool("notes", "Your notes - your memory across chats. save: a decision, preference, constraint, conclusion or "
       "fact worth keeping (not tool output); your 5 latest notes stay in view in every chat, older ones are found "
       "by search. list: every note, when the user asks what you remember. delete: one note, when the user asks you "
       "to forget it - the user confirms with a button, so call it straight away.",
       NotesIn, lambda c, a: notes.notes(c.ws, a.mode, a.title, a.content, index=c.index),
       labels=("Working with notes", "Worked with notes"),
       mode_labels={"save": ("Saving a note", "Saved a note"), "list": ("Looking at your notes", "Looked at your notes"),
                    "delete": ("Asking to delete a note", "Asked to delete a note")}),
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
    Tool("web_search", "Search the web: titles, links and snippets.",
       SearchIn, lambda c, a: web.web_search(a.query, a.site, recent=a.recent,
                                             embedder=getattr(c.index, "embedder", None)),
       labels=("Searching the web", "Searched the web")),
    Tool("read_webpage", "Read the text of one web page (HTML, text, or a PDF, Word or PowerPoint file; a data file "
       "shows its columns and first rows). A YouTube link gives the video's title, channel, length, date and "
       "description (not what is said in it).", ReadPageIn,
       lambda c, a: _read_page(c, a),
       labels=("Reading a page", "Read a page")),
    Tool("files", "The workspace's files (notes are not files). Use it when the user asks what files they have, or "
       "to open, show, save, change, rename, delete or download one. list: everything, a page at a time; find: whether "
       "a name exists; read: a file's report (what a helper did with it), or its text from line start on (an "
       "image or a table says which tool reads it); write: a new file the user asked for (.md, .txt, .csv, .json, "
       ".py ...; a Word .docx or slides .pptx are made from Markdown - for slides, each # heading starts a slide - and "
       "a line that is only ![caption](image_7) puts in that picture or chart); update: change an existing file - its "
       "whole text, or lines "
       "start-end; rename; delete: one "
       "file; download: one file from GitHub, or a data table (CSV, Excel) from any public link. Delete, "
       "download and updating the user's own file ask the user with a button - call it straight away, don't ask "
       "first in words.",
       FilesIn, lambda c, a: file_ops.files(c.ws, c.index, a.mode, name=a.name, page=a.page, start=a.start,
                                            end=a.end, content=a.content, new_name=a.new_name, link=a.link),
       labels=("Working with files", "Worked with files"),
       mode_labels={"list": ("Listing files", "Listed files"), "find": ("Looking for a file", "Looked for a file"),
                    "read": ("Reading a file", "Read a file"), "write": ("Writing a file", "Wrote a file"),
                    "update": ("Updating a file", "Updated a file"), "rename": ("Renaming a file", "Renamed a file"),
                    "delete": ("Asking to delete a file", "Asked to delete a file"),
                    "download": ("Asking to download", "Asked to download")}),
    Tool("search_files", "Find passages in the user's documents and saved notes that match a query.", FilesQueryIn,
       lambda c, a: files.search_files(c.index, a.query),
       labels=("Searching your documents and notes", "Searched your documents and notes")),
    Tool("recall", "Find what was said in earlier conversations - and earlier in this one, when it is out of view.",
       FilesQueryIn,
       lambda c, a: memory.recall(c.index, a.query, exclude_conversation=c.conversation, visible_since=c.visible_since),
       labels=("Searching earlier chats", "Searched earlier chats")),
    Tool("read_file", "Read a stored file or note in order, from a line on (code with line numbers, documents with "
       "their pages), with what a helper reported about it.", ReadFileIn,
       lambda c, a: read_file.read_file(c.ws, a.file, a.start),
       labels=("Reading a file", "Read a file")),
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
    Tool("make_chart", "Draw a chart of a table, computed from all its rows (also kept as a picture - its id "
       "comes back as picture - for documents and slides): line (over time; by = one line per "
       "group), bar (by category; by = grouped or stacked), scatter (two numbers, with a trend line), histogram "
       "(how one number spreads), box (a number's spread per group), heatmap (two categories, e.g. weekday x hour) "
       "or pie (shares, up to 5 slices). It groups and sums (agg) itself: no need to aggregate with query_data "
       "first.",
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
    Tool("run_code", "Write your scratch Python script (the whole of it) and run it. In the script, "
       "table('data_1') gives a table as a DataFrame, open('file_3') opens a stored item by its id "
       "(Image.open('image_3') an image), and path('file_3') gives its location for libraries that open files "
       "themselves (pandas, openpyxl). Files the script writes are kept as new items (.csv / .parquet become tables, "
       "images are shown); a stored item is never changed - write under a new name. No internet. "
       "Libraries: " + ", ".join(sandbox.INSTALLED) + ".", RunCodeIn,
       lambda c, a: scratch.run_code(c.ws, c.scratch, a.code, a.timeout, a.input),
       labels=("Running code", "Ran code")),
    Tool("fix_code", "Change lines from_line-to_line of your scratch script and run it again - after an error, "
       "the lines it shows. Cheaper and safer than sending the whole script again.", FixCodeIn,
       lambda c, a: scratch.fix_code(c.ws, c.scratch, a.from_line, a.to_line, a.new_code, a.timeout, a.input),
       labels=("Fixing the code", "Fixed the code")),
]}


def execute(ctx: ToolContext, name: str, raw_input, tools: dict[str, Tool] | None = None) -> ToolResult:
    """Validate and run one call (from ``tools``, default every registered tool); artifacts go to the
    UI through ctx.emit."""
    tool = (tools if tools is not None else TOOLS).get(name)
    if tool is None:
        return ToolResult("invalid_arguments", {"error": {"message": f"There is no tool '{name}'."}})
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
# runs, so every tool takes an id, a name or a copied listing line alike (Workspace.resolve).
ITEM_FIELDS = {"data": ("table",), "new_data": ("table",), "image": ("image",)}
ITEM_FIELDS_BY_INPUT = {ReadFileIn: {"file": read_file.KINDS}}


def _resolve_items(ctx: ToolContext, args: _In) -> None:
    """Every item named in a call, as its id (in place); a ToolError the model can act on when it isn't there."""
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
