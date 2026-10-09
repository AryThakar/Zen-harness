"""What each helper is, defined once: its result shape (what it hands main), its profile (instructions,
tools, step limit, the check its result must pass) and how main and the user know it (Delegate)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.tools import ask
from app.tools.core import ToolResult

from .. import prompts
from ..registry import TOOLS, AskIn, CodeTaskIn, DataTaskIn, QuestionIn, TaskIn, Tool, ToolContext
from .checks import _check_data, _check_knowledge, _check_save, _check_web

if TYPE_CHECKING:
    from .evidence import Trace


SUBMIT = "submit_result"


MAX_ASKS = 2  # questions a helper may ask the user in one task


HELPER_ASK = ("Ask the user one question and wait for the answer - you keep your work and go on. Only when the "
              "task leaves a choice that changes the result (which chart, which table or column, which of two "
              "readings, a file format) and the task doesn't already say it. Give the choices as options, the "
              "recommended one first; at most 2 questions a task.")


class _Out(BaseModel):
    """Every worker can also brief the main slot: what more could be looked into, or a question
    only the user can answer (the main slot asks it, then hands the task back with the answer)."""
    model_config = ConfigDict(extra="forbid")
    suggestion: str | None = Field(None, description="optional, when the answer is incomplete: one sentence on "
                                   "what else could be looked into, or what only the user can tell")
    question_for_user: str | None = Field(None, description="a question only the user can answer (or one a tool "
                                          "returned): ask it here instead of guessing")
    options: list[str] | None = None


BRIEF = ("suggestion", "question_for_user", "options")


class SourcedResult(_Out):
    answer: str = Field(description="the answer to the task")
    sources: list[str] = Field(default_factory=list, description="the sources used, exactly as the tools gave them")


class WebResult(SourcedResult):
    downloads: list[str] = Field(default_factory=list, description="links to files the task asks for (a data table, "
                                 "a GitHub file), exactly as a page gave them - main downloads them with the user's "
                                 "approval; never paste a file's content into the answer")


class DataReport(_Out):
    """The data analyst's finish: its report, kept on the table it worked on and shown at the start of
    the next task on that table."""
    question: str = Field(description="one line: what was asked (the report's title)")
    data_used: str = Field(description="the tables used and the cleaning decisions, e.g. 'use data_4: data_1 with "
                                       "the Total row removed, dates read day-first'")
    method: str = Field(description="the tools and queries, in order, in plain words")
    results: str = Field(description="the answer to the task")
    caveats: str = Field(description="limits of the data or the method, and what to look at next")


class SaveResult(_Out):
    """The coder's finish: its scratch file kept under a name, with the report main answers from."""
    name: str = Field(description="the file's name with the right extension, e.g. 'parabola.py' (any language: "
                                   "'hello.rs', 'page.html')")
    purpose: str = Field(description="one line: what the file does (the first line of its report)")
    how_it_works: str = Field(description="its parts and flow, naming its functions as `name()`")
    inputs_outputs: str = Field(description="what it reads and what it prints or writes")
    how_to_run: str = Field(description="how the user runs it (python name.py; an .html file opens in a browser), "
                                         "and how to change the usual things")
    limits: str = Field(description="what it doesn't do yet, and sensible next steps")
    code: str | None = Field(None, description="only for a file that isn't Python: its whole content (Python is "
                                               "saved from your scratch script, as it ran)")


@dataclass(frozen=True)
class Profile:
    name: str
    instructions: str
    tools: tuple[str, ...]
    max_steps: int
    result_model: type[_Out]
    check: Callable[[_Out, Trace, str], list[str]]
    finish: str = SUBMIT  # the tool that hands in the result (the coder's is save)
    asks: bool = False  # may ask the user directly (ask_user, in place)
    sees_images: bool = False  # is shown pictures in its conversation (when the server has vision)

    def own_tools(self) -> dict[str, Tool]:
        tools = {n: TOOLS[n] for n in self.tools}
        if self.asks:
            tools["ask_user"] = _ask_tool(DELEGATE_OF[self.name])
        return tools


def _ask_tool(delegate: str) -> Tool:
    """A helper's ask_user: the card in the chat, the helper waiting in place for the answer."""
    def handler(ctx: ToolContext, args: AskIn) -> ToolResult:
        built = ask.ask_user(args.question, args.options, args.allow_text)  # the same checks as main's
        if built.status != "ok":
            return built
        card = {k: v for k, v in built.artifact.items() if k != "type"}
        options = card["options"]
        default = f" ({options[0]})" if options else ""
        if ctx.ask is None:  # no user to ask here (a benchmark, a test)
            return ToolResult("ok", {"answer": None, "note": f"No user can be asked now: go on with your recommended "
                                                             f"option{default} and say so in the report."})
        got = ctx.ask({"tool": delegate, **card})
        if got.get("answer"):
            return ToolResult("ok", {"question": card["question"], "answer": got["answer"]})
        why = "The user skipped the question" if got.get("skipped") else "The user didn't answer in time"
        return ToolResult("ok", {"question": card["question"], "answer": None,
                                 "note": f"{why}: go on with your recommended option{default} and say so in the report."})
    return Tool("ask_user", HELPER_ASK, AskIn, handler, labels=TOOLS["ask_user"].labels)


PROFILES = {p.name: p for p in [
    Profile("web", prompts.WEB, ("web_search", "read_webpage"), 12, WebResult, _check_web, asks=True),
    Profile("knowledge", prompts.KNOWLEDGE, ("search_files", "recall", "read_file"), 6, SourcedResult, _check_knowledge),
    Profile("data", prompts.DATA,
            ("inspect_data", "clean_data", "query_data", "make_chart", "compare", "forecast", "predict",
             "find_patterns", "analyze_text", "run_code", "fix_code"), 10, DataReport, _check_data,
            finish="save_report", asks=True),
    Profile("coder", prompts.CODER, ("run_code", "fix_code", "read_file"), 10, SaveResult, _check_save,
            finish="save", asks=True, sees_images=True),
]}


@dataclass(frozen=True)
class Delegate:
    """A helper as main and the user know it, defined once: the tool main calls, its input, what it does and
    gives back (the description main reads), and how the UI names it while it works (helper, labels)."""
    profile: str
    input_model: type
    description: str
    helper: str
    labels: tuple[str, str]


DELEGATES = {
    "research_web": Delegate("web", TaskIn, "Research a question on the web: a helper searches, reads pages and returns a cited answer, dated for "
                     "'latest' questions, how each cited page was read, the pages it could not read and why, and the links of "
                     "files the task asks for (a dataset, a script) for you to download. Use it for what is current or what "
                     "you don't know - news, releases, prices, people, events - and to read a page the user links to. Not for "
                     "the user's own files (ask_knowledge), or for bringing in a file whose link you have (files, "
                     "mode='download').",
                             "Web researcher", ("Researching on the web", "Researched on the web")),
    "ask_knowledge": Delegate("knowledge", QuestionIn, "Answer from the user's own documents, saved notes and earlier "
                      "conversations, with sources. Use it when the user asks about their documents or notes, what was "
                      "said or decided before, or for a summary of an attached document. The user's own files "
                      "come first: when a document or note in the workspace may cover the question (judge by its "
                      "name and title), ask here before research_web.",
                              "Knowledge", ("Looking through your files and chats", "Looked through your files and chats")),
    "analyze_data": Delegate("data", DataTaskIn, "Give a data task to the data analyst. Use it for any question about the numbers in the user's tables: "
                     "cleaning, exact numbers, charts, whether a difference or A/B result is real, trends and forecasts, what "
                     "predicts or drives a column, customer segments, unusual days, items bought together, themes in text. Not "
                     "for numbers typed in the chat (calculate), or data that isn't a table in the workspace yet (bring it in "
                     "first: files, mode='download').",
                             "Data analyst", ("Analysing your data", "Analysed your data")),
    "write_code": Delegate("coder", CodeTaskIn, "Have the coder write one file and keep it - a Python script it runs, tests and fixes, or a file in any "
                     "language - or change an existing one (give its name as file). Use it when the user asks for code, or the "
                     "work needs a program: simulations, conversions, editing a picture, a plot of a formula. Not for a sum "
                     "you can calculate, a table's numbers or charts (analyze_data), or documents - Word, slides, Markdown, "
                     "text (files, mode='write'). You get its report, never the code: answer from the report and never paste "
                     "or retype code; the user sees the file in the chat.",
                           "Coder", ("Writing and running code", "Wrote and ran code")),
}


DELEGATE_OF = {d.profile: name for name, d in DELEGATES.items()}  # helper -> main's tool for it


MAIN_DIRECT = ("get_weather", "generate_image", "view_image", "draw_diagram", "calculate", "notes", "plan",
               "ask_user", "files")
