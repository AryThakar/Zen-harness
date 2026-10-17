"""What each helper is, defined once: its result shape (what it hands main), its profile (instructions,
tools, step limit, the check its result must pass) and how main and the user know it (Delegate).

Three helpers, one job each, so main never has to guess between two that could both do a task:
    web        the internet: search, read pages, hand back cited answers and file links
    knowledge  the user's own files and earlier chats
    engineer   computing in the workspace: the numbers in tables (the data tools) and programs - Python files it
               writes, runs and edits in the workspace (files, run); it finishes with finish
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
from ..registry import HELPER_TOOLS, TOOLS, AskIn, QuestionIn, TaskIn, Tool, ToolContext, WorkTaskIn
from .checks import _check_knowledge, _check_web, _check_work

if TYPE_CHECKING:
    from .evidence import Trace


SUBMIT = "submit_result"  # the web and knowledge helpers' finish
FINISH = "finish"  # the engineer's


MAX_ASKS = 2  # questions a helper may ask the user in one task


HELPER_ASK = ("Ask the user one question and wait - you keep your work and go on. Returns their answer, or that "
              "they skipped it. Use it only when the task leaves a choice that changes the result (which chart, which "
              "table or column, which of two readings, a file format) and the task doesn't already say it. Give the "
              "choices as options, the recommended one first; at most 2 questions a task.")


class _Out(BaseModel):
    """One envelope for every helper's finish. The model writes only what only it knows - the answer, its caveats,
    and a briefing when it needs one (what else could be looked into, a question only the user can answer: main
    asks it, then hands the task back with the answer). What the app saw - what ran, the files made or changed, the
    pages read - the app adds itself (runner._done): the model is never asked for, or refused over, a fact the app
    already has (measured: a correct answer refused twice for a missing 'method')."""
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(description="the answer to the task")
    caveats: str | None = Field(None, description="optional: what limits the answer (the data, the method), what is "
                                                  "uncertain, what to look at next")
    suggestion: str | None = Field(None, description="optional, when the answer is incomplete: one sentence on "
                                   "what else could be looked into, or what only the user can tell")
    question_for_user: str | None = Field(None, description="a question only the user can answer (or one a tool "
                                          "returned): ask it here instead of guessing")
    options: list[str] | None = Field(None, description="the choices for question_for_user, the recommended one first")


BRIEF = ("suggestion", "question_for_user", "options")


class SourcedResult(_Out):
    """The knowledge helper's finish."""
    sources: list[str] = Field(default_factory=list, description="the sources used, exactly as the tools gave them")


class WebResult(_Out):
    """The web helper's finish: pages cited by the number each read gave them - the app writes their links."""
    sources: list[int | str] = Field(default_factory=list, description="the numbers of the pages you used, e.g. [1, 3]: "
                                     "each page you read gives its number")
    downloads: list[str] = Field(default_factory=list, description="links to files the task asks for (a data table, "
                                 "a GitHub file) - or to a page whose table holds the data the task needs (a price "
                                 "history, results): main brings that table in as data - exactly as a page gave "
                                 "them, with the user's approval; never copy a file's or a table's rows into the "
                                 "answer")


class WorkResult(_Out):
    """The analyst-programmer's finish: what main answers from - and, when the task was about a table, the report
    kept on it for the next task (the task as its title, the app's facts after)."""


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
    task_lead: str = "Your task"  # the words before main's task in the opening
    sampling: dict | None = None  # its own sampling over the server's, on every call

    def own_tools(self) -> dict[str, Tool]:
        tools = {t.name: t for t in (HELPER_TOOLS.get(n) or TOOLS[n] for n in self.tools)}  # by the name it is called
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

# Qwen 3.6's model card, thinking mode: precise coding wants temperature 0.6 and no presence penalty; the server runs
# its general set (1.0, presence 1.5 - the Kaggle notebook). The analyst writes code and SQL, so it asks for these.
PRECISE = {"temperature": 0.6, "presence_penalty": 0.0}

PROFILES = {p.name: p for p in [
    Profile("web", prompts.WEB, ("web_search", "read_webpage"), 12, WebResult, _check_web, asks=True),
    Profile("knowledge", prompts.KNOWLEDGE, ("files:read", "recall"), 6, SourcedResult, _check_knowledge),
    # main's task says what is wanted; the way is the helper's - measured: it followed main's code steps in scripts
    # and its tested tools went unused (checks.dictated_how stops the worst at main's call)
    Profile("engineer", prompts.ENGINEER, (*DATA_TOOLS, "files", "run"), 12, WorkResult, _check_work, finish=FINISH,
            asks=True, sees_images=True,
            task_lead="Your task - what is wanted; the way is yours, your tested tools first, even where it names steps",
            sampling=PRECISE),
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
    "research_web": Delegate("web", TaskIn, "Research a question on the web: a helper searches and reads pages. "
                     "Returns a cited answer - dated for 'latest' questions - with its caveats, how each cited page was "
                     "read, the pages it could not read and why, and the links of files the task asks for (a dataset, a "
                     "script) or of a page whose table holds the data, for you to bring in with files(mode='download'). Use it for what is current or what you don't know - news, releases, "
                     "prices, people, events - to find a public file or dataset, and to read a page the user links to. "
                     "Not for the user's own files (files, ask_knowledge), or for bringing in a file whose link you have "
                     "(files, mode='download').",
                             "Web researcher", ("Researching on the web", "Researched on the web")),
    "ask_knowledge": Delegate("knowledge", QuestionIn, "Answer from the user's own files and earlier "
                      "conversations: a helper finds and reads them in its own context. Returns an answer with its "
                      "sources. Use it for reading many or long documents (a summary of a long report, a comparison "
                      "across files) and for what was said or decided in earlier chats. Not for a quick lookup - "
                      "which files, one passage, a pinned file: that is yours with files (find, read) - or computing "
                      "on a table (analyze_or_code).",
                              "Knowledge", ("Looking through your files and chats", "Looked through your files and chats")),
    "analyze_or_code": Delegate("engineer", WorkTaskIn, "Give a task to the analyst-programmer: a helper that "
                     "works on the workspace's files with tested data and machine-learning tools and with Python files "
                     "it writes, runs and edits. Say what the user wants and why - the result, not how to compute it: "
                     "it picks the tools itself; name the files the task is about in files. Returns its answer and "
                     "caveats, what it ran, and the files it made or changed - the user sees them in the chat: answer "
                     "from what it says, never retype code. It starts each task fresh: what it did before is in that "
                     "earlier result. Use it for the numbers in the user's tables - cleaning, exact figures, charts, "
                     "statistical tests (is a difference, a link or an A/B result real), trends and forecasts, machine "
                     "learning (classification or regression: what predicts or drives a column, predictions), "
                     "clustering customers into segments, unusual days, items bought together, themes in text - and "
                     "for programs: a script it runs and tests, a simulation, a game, a conversion, editing a picture, "
                     "a file in another language. Not for maths typed in the chat (calculate), documents - Word, "
                     "slides, Markdown (files, mode='write', with its charts' pictures by name) - or anything on the "
                     "web: it has no internet, so a web file is brought in first (files, mode='download').",
                             "Analyst & coder", ("Working on it", "Worked on it")),
}


DELEGATE_OF = {d.profile: name for name, d in DELEGATES.items()}  # helper -> main's tool for it


MAIN_DIRECT = ("get_weather", "generate_image", "view_image", "draw_diagram", "calculate", "ask_user", "files")
