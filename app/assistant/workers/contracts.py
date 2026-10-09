"""What each helper is, defined once: its result shape (what it hands main), its profile (instructions,
tools, step limit, the check its result must pass) and how main and the user know it (Delegate).

Three helpers, one job each, so main never has to guess between two that could both do a task:
    web        the internet: search, read pages, hand back cited answers and file links
    knowledge  the user's own documents, notes and earlier chats
    engineer   computing in the workspace: the numbers in tables (the data tools) and programs (one scratch
               Python script); it keeps a program with save_code and finishes with finish
The data analyst and the coder were two helpers until 2026-10-10: both ran Python on the same workspace, and
main sent data work to the coder (a download it couldn't do, a demo dataset) and the analyst built slide decks -
the split was by what came back, which main had to predict. One helper with both crafts has no such seam."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.tools import ask
from app.tools.core import ToolResult

from .. import prompts
from ..registry import TOOLS, AskIn, QuestionIn, TaskIn, Tool, ToolContext, WorkTaskIn
from .checks import _check_knowledge, _check_web, _check_work

if TYPE_CHECKING:
    from .evidence import Trace


SUBMIT = "submit_result"  # the web and knowledge helpers' finish
FINISH = "finish"  # the engineer's
SAVE_CODE = "save_code"  # the engineer keeps a program (a tool, not its finish)


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
    options: list[str] | None = Field(None, description="the choices for question_for_user, the recommended one first")


BRIEF = ("suggestion", "question_for_user", "options")


class SourcedResult(_Out):
    answer: str = Field(description="the answer to the task")
    sources: list[str] = Field(default_factory=list, description="the sources used, exactly as the tools gave them")


class WebResult(SourcedResult):
    downloads: list[str] = Field(default_factory=list, description="links to files the task asks for (a data table, "
                                 "a GitHub file), exactly as a page gave them - main downloads them with the user's "
                                 "approval; never paste a file's content into the answer")


class WorkResult(_Out):
    """The engineer's finish: what main answers from, and - when the task was about a table - the report kept
    on that table for the next task (reports.DATA_SECTIONS: the task is its title, the app adds the facts)."""
    answer: str = Field(description="the answer to the task: the results, or what you made and what its run showed")
    method: str = Field(description="what you did, in order, in plain words: the tools, queries and scripts")
    caveats: str = Field(description="limits of the data or the method, and what to look at next")
    data_used: str | None = Field(None, description="when you used tables: which, and the cleaning decisions, e.g. "
                                                    "'data_4: data_1 with the Total row removed, dates day-first'")


class SaveCodeIn(BaseModel):
    """save_code: the scratch script (or a file in another language) kept under a name, with its report."""
    model_config = ConfigDict(extra="forbid")
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


SAVE_CODE_DESCRIPTION = ("Keep your scratch script in the workspace as a file, with its report - main and the next "
                         "helper know the file only from the report: say what the code does and what its last run "
                         "printed, nothing else. Python is run before it is kept. Call it when the task wants a "
                         "program kept, then finish.")


@dataclass(frozen=True)
class Profile:
    name: str
    instructions: str
    tools: tuple[str, ...]
    max_steps: int
    result_model: type[_Out]
    check: Callable[[_Out, Trace, str], list[str]]
    finish: str = SUBMIT  # the tool that hands in the result
    asks: bool = False  # may ask the user directly (ask_user, in place)
    sees_images: bool = False  # is shown pictures in its conversation (when the server has vision)
    saves_code: bool = False  # keeps programs with save_code (the scratch script, with its report)

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
                                                             f"option{default} and say so in your answer."})
        got = ctx.ask({"tool": delegate, **card})
        if got.get("answer"):
            return ToolResult("ok", {"question": card["question"], "answer": got["answer"]})
        why = "The user skipped the question" if got.get("skipped") else "The user didn't answer in time"
        return ToolResult("ok", {"question": card["question"], "answer": None,
                                 "note": f"{why}: go on with your recommended option{default} and say so in your answer."})
    return Tool("ask_user", HELPER_ASK, AskIn, handler, labels=TOOLS["ask_user"].labels)


DATA_TOOLS = ("inspect_data", "clean_data", "query_data", "make_chart", "compare", "forecast", "predict",
              "find_patterns", "analyze_text")

PROFILES = {p.name: p for p in [
    Profile("web", prompts.WEB, ("web_search", "read_webpage"), 12, WebResult, _check_web, asks=True),
    Profile("knowledge", prompts.KNOWLEDGE, ("search_files", "recall", "read_file"), 6, SourcedResult, _check_knowledge),
    Profile("engineer", prompts.ENGINEER, (*DATA_TOOLS, "run_code", "fix_code", "read_file"), 12, WorkResult,
            _check_work, finish=FINISH, asks=True, sees_images=True, saves_code=True),
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


# Each says what the helper does, what it gives back, when to use it and when not (with the tool instead):
# main knows its helpers only by these.
DELEGATES = {
    "research_web": Delegate("web", TaskIn, "Research a question on the web: a helper searches, reads pages and returns "
                     "a cited answer, dated for 'latest' questions, how each cited page was read, the pages it could not "
                     "read and why, and the links of files the task asks for (a dataset, a script) for you to download. "
                     "Use it for what is current or what you don't know - news, releases, prices, people, events - to "
                     "find a public file or dataset, and to read a page the user links to. Not for the user's own files "
                     "(ask_knowledge), or for bringing in a file whose link you have (files, mode='download').",
                             "Web researcher", ("Researching on the web", "Researched on the web")),
    "ask_knowledge": Delegate("knowledge", QuestionIn, "Answer from the user's own documents, saved notes and earlier "
                      "conversations, with sources. Use it when the user asks about their documents or notes, what was "
                      "said or decided before, or for a summary of an attached document. The user's own files "
                      "come first: when a document or note in the workspace may cover the question (judge by its "
                      "name and title), ask here before research_web. Not for computing on a table (analyze_or_code).",
                              "Knowledge", ("Looking through your files and chats", "Looked through your files and chats")),
    "analyze_or_code": Delegate("engineer", WorkTaskIn, "Give a task to the analyst-programmer, who works in the "
                     "workspace with data tools and Python. It returns its answer, a report, and the tables, charts and "
                     "files it made - the user sees them in the chat; you get the report, never the code: answer from it "
                     "and never paste or retype code. Use it for the numbers in the user's tables - cleaning, exact "
                     "figures, charts, whether a difference or an A/B result is real, trends and forecasts, what predicts "
                     "a column, customer segments, unusual days, items bought together, themes in text - and for "
                     "programs: a script it runs and tests, a simulation, a game, a conversion, editing a picture, a "
                     "plot of a formula, a file in another language. Not for numbers typed in the chat (calculate), "
                     "documents - Word, slides, Markdown (files, mode='write', with the picture ids of its charts) - or "
                     "anything on the web: it has no internet, so a web file is brought in first (files, "
                     "mode='download').",
                             "Analyst & coder", ("Working on it", "Worked on it")),
}


DELEGATE_OF = {d.profile: name for name, d in DELEGATES.items()}  # helper -> main's tool for it


MAIN_DIRECT = ("get_weather", "generate_image", "view_image", "draw_diagram", "calculate", "notes", "plan",
               "ask_user", "files")
