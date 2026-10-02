"""Every tool the models can call: an input schema (pydantic -> JSON schema, the model sees the
descriptions) and a handler. A call is validated against its schema before anything runs; a
wrong argument comes back as an invalid_arguments result the model can fix.

Descriptions say what a tool is for and, where a model could misuse it, what it is NOT for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.tools import (ask, calc, clean_data, diagram, files, forecast, image, inspect_data, make_chart, memory, ml,
                       notes, query_data, read_file, sandbox, stats_test, weather, web)
from app.tools.core import ToolResult
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@dataclass
class ToolContext:
    ws: Workspace
    index: SearchIndex
    conversation: str = "default"
    emit: Callable[[dict], None] = field(default=lambda artifact: None)  # artifacts go to the UI


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[_In]
    handler: Callable[[ToolContext, Any], ToolResult]

    def spec(self) -> dict:
        schema = self.input_model.model_json_schema()
        _strip_titles(schema)
        return {"name": self.name, "description": self.description, "input_schema": schema}


# ---------------------------------------------------------------- inputs

class WeatherIn(_In):
    location: str | None = Field(None, description="a place, e.g. 'Pune' or 'Paris, France'; omit for the user's location")
    days: int = Field(0, ge=0, le=7, description="0 = now only; 1-7 = also a daily forecast from today")


class ImageIn(_In):
    prompt: str = Field(description="a detailed visual description of the image")
    size: Literal["square", "wide", "tall"] = "square"


class DiagramIn(_In):
    mermaid: str = Field(description="Mermaid code, first line the diagram type, e.g. 'flowchart TD'")
    title: str | None = None


class CalcIn(_In):
    expression: str = Field(description="arithmetic, e.g. '2400 * 0.15'; functions: round abs min max sqrt log log10 exp")


class NoteIn(_In):
    title: str = Field(description="short title; saving again with the same title updates the note")
    content: str = Field(description="the decision, constraint, conclusion or summary, in markdown")
    pin: bool = Field(False, description="ask the user to pin it so it is shown at the start of every turn")


class AskIn(_In):
    question: str = Field(description="one short question")
    options: list[str] | None = Field(None, description="choices to show as buttons (the user can also type or skip)")
    allow_text: bool = True


class TaskIn(_In):
    task: str = Field(description="what to do, complete and self-contained (the helper does not see this chat)")


class DataTaskIn(TaskIn):
    data: str | None = Field(None, description="the table id to work on, e.g. 'data_1', if the task is about one table")


class QuestionIn(_In):
    question: str = Field(description="what to find in the user's documents, notes and earlier chats")


class SearchIn(_In):
    query: str = Field(description="what to look for, in words a page would use")
    site: str | None = Field(None, description="only this domain, e.g. 'python.org'")


class ReadPageIn(_In):
    url: str
    find: str | None = Field(None, description="for a long page: words to look for; the parts about them are shown")


class FilesQueryIn(_In):
    query: str = Field(description="what to look for, in words the document would use")


class ReadFileIn(_In):
    file: str = Field(description="a file or note id, e.g. 'file_1'")
    part: int = Field(1, ge=1, description="which part, from 1; the result says how many there are")


class DataIn(_In):
    data: str = Field(description="table id, e.g. 'data_1'")


class CleanIn(_In):
    data: str
    steps: list[dict] = Field(description="steps in order, each {'op': ..., ...}: parse_numbers{columns}, "
                              "parse_dates{columns, order: auto|dayfirst|monthfirst}, drop_duplicates{columns?}, "
                              "drop_rows{column, values}, fill_missing{columns, method: mean|median|mode|zero|value|forward, value?}, "
                              "drop_missing{columns?}, tidy_text{columns}, flag_outliers{columns}, remove_outliers{columns}, "
                              "rename_columns{mapping}, drop_columns{columns}")


class SqlIn(_In):
    sql: str = Field(description="one DuckDB SELECT; tables are named by id, e.g. SELECT region, sum(amount) FROM data_2 GROUP BY 1")
    save: bool = Field(False, description="save the full result as a new table (to chart, test or model it)")


class ChartIn(_In):
    data: str
    type: Literal["line", "bar", "scatter", "histogram"]
    x: str
    y: str | None = Field(None, description="number column; omit to count rows (line, bar); not used by histogram")
    agg: Literal["sum", "mean", "median", "count", "min", "max"] = "sum"
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto"
    top: int = Field(20, ge=1, le=50)
    title: str | None = None


class StatsIn(_In):
    data: str
    question: Literal["difference", "correlation", "association"] = Field(
        description="difference: a number across groups; correlation: two numbers; association: two categories")
    columns: list[str] = Field(min_length=2, max_length=2)


class TrainIn(_In):
    data: str
    target: str = Field(description="the column to predict")
    features: list[str] | None = Field(None, description="columns to learn from; omit for all usable ones")
    time_column: str | None = None


class PredictIn(_In):
    model: str = Field(description="model id, e.g. 'model_1'")
    data: str = Field(description="table with the new rows")


class ForecastIn(_In):
    data: str
    date: str
    value: str | None = Field(None, description="number column; omit to forecast the row count")
    horizon: int | None = Field(None, ge=1, le=60)
    time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto"
    agg: Literal["sum", "mean", "last", "count"] = "sum"


class PythonIn(_In):
    code: str = Field(description="a complete Python script; print what you need; table('data_1') and path('file_1') are available")
    timeout: int = Field(30, ge=1, le=120)


# ---------------------------------------------------------------- the tools

def _t(name, description, model, handler) -> Tool:
    return Tool(name, description, model, handler)


TOOLS: dict[str, Tool] = {t.name: t for t in [
    _t("get_weather", "Current weather, UV, air quality and local time for a place, or a daily forecast up to 7 days.",
       WeatherIn, lambda c, a: weather.get_weather(a.location, a.days)),
    _t("generate_image", "Generate one image from a detailed description; it is shown to the user.",
       ImageIn, lambda c, a: image.generate_image(c.ws, a.prompt, a.size)),
    _t("draw_diagram", "Draw a flowchart, sequence, class, state, ER, gantt, pie, mindmap or timeline diagram from Mermaid code.",
       DiagramIn, lambda c, a: diagram.draw_diagram(a.mermaid, a.title)),
    _t("calculate", "Exact arithmetic. Use it for every calculation instead of doing sums yourself.",
       CalcIn, lambda c, a: calc.calculate(a.expression)),
    _t("save_note", "Save a decision, constraint, conclusion or summary worth keeping (not tool output).",
       NoteIn, lambda c, a: notes.save_note(c.ws, a.title, a.content, a.pin, index=c.index)),
    _t("ask_user", "Ask the user one question, only when reasonable readings would give materially different "
       "results. Never ask about preferences that don't change the answer.",
       AskIn, lambda c, a: ask.ask_user(a.question, a.options, a.allow_text)),
    # worker tools
    _t("web_search", "Search the web: titles, links and snippets. Snippets are not enough to quote; read the page.",
       SearchIn, lambda c, a: web.web_search(a.query, a.site)),
    _t("read_webpage", "Read the text of one web page (HTML, text or PDF). A YouTube link gives the video's title, "
       "channel, length, date and description (not what is said in it).", ReadPageIn,
       lambda c, a: web.read_webpage(a.url, a.find)),
    _t("search_files", "Find passages in the user's documents and saved notes that match a query.", FilesQueryIn,
       lambda c, a: files.search_files(c.index, a.query)),
    _t("recall", "Find what was said in earlier conversations.", FilesQueryIn,
       lambda c, a: memory.recall(c.index, a.query, exclude_conversation=c.conversation)),
    _t("read_file", "Read a stored file or note from start to end, in parts (for summaries and code).", ReadFileIn,
       lambda c, a: read_file.read_file(c.ws, a.file, a.part)),
    _t("inspect_data", "First look at a table: columns, types, missing values, duplicates, outliers, suggested "
       "cleaning steps and questions only the user can answer.", DataIn,
       lambda c, a: inspect_data.inspect_data(c.ws, a.data)),
    _t("clean_data", "Apply cleaning steps to a table; saves a new table and a log. Nothing is changed without a step.",
       CleanIn, lambda c, a: clean_data.clean_data(c.ws, a.data, a.steps)),
    _t("query_data", "Exact numbers with one read-only SQL query: totals, rankings, filters, group-bys.",
       SqlIn, lambda c, a: query_data.query_data(c.ws, a.sql, a.save)),
    _t("make_chart", "Draw a line (over time), bar (by category), scatter or histogram chart of a table. For a "
       "chart over time give the date column as x and a time_unit: it sums (agg) per period itself, so there is "
       "no need to aggregate with query_data first.",
       ChartIn, lambda c, a: make_chart.make_chart(c.ws, a.data, a.type, a.x, a.y, a.agg, a.time_unit, a.top, a.title)),
    _t("stats_test", "Is a difference, correlation or association real or chance? The right test is chosen for you.",
       StatsIn, lambda c, a: stats_test.stats_test(c.ws, a.data, a.question, a.columns)),
    _t("train_model", "Train and test a model that predicts a column; reports quality against a baseline, leaks "
       "and important predictive features (associations, not causes).", TrainIn,
       lambda c, a: ml.train_model(c.ws, a.data, a.target, a.features, a.time_column)),
    _t("predict", "Apply a trained model to a table of new rows.", PredictIn,
       lambda c, a: ml.predict(c.ws, a.model, a.data)),
    _t("forecast", "Forecast a value over time with a range, tested against a simple baseline.", ForecastIn,
       lambda c, a: forecast.forecast(c.ws, a.data, a.date, a.value, a.horizon, a.time_unit, a.agg)),
    _t("run_python", "Run a Python script for what no other tool does. Do NOT use it for cleaning, SQL, charts, "
       "statistical tests, training or forecasting - those tools are tested and exact.", PythonIn,
       lambda c, a: sandbox.run_python(c.ws, a.code, a.timeout)),
]}


def execute(ctx: ToolContext, name: str, raw_input) -> ToolResult:
    """Validate and run one call; artifacts go to the UI through ctx.emit."""
    tool = TOOLS.get(name)
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
    result = tool.handler(ctx, args)
    if result.artifact:
        ctx.emit({"tool": name, **result.artifact})
    return result


def specs(names: list[str]) -> list[dict]:
    return [TOOLS[n].spec() for n in names]


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
