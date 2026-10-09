"""Worker sub-agents: the second model slot doing one focused task with a narrow toolset.

The main slot delegates with research_web / ask_knowledge / analyze_data / write_code. Each runs a
worker profile - its own short instructions, only its own tools, a step limit - on the worker
slot, and returns a short structured result instead of raw pages or tables - one shape for all four:
answer (what main tells the user from), sources, report, files (id, name, kind of what it saved or made)
and the briefing below.

    web        web_search, read_webpage, ask_user                       -> answer, sources (+ download links)
    knowledge  search_files, recall, read_file                          -> answer, sources
    data       the 9 data tools, run_code, fix_code, ask_user           -> save_report: answer, report, files
    coder      run_code, fix_code, read_file, ask_user                  -> save: answer, report, files (its file first)
(run_code / fix_code: the helper's one scratch script, written whole or changed by lines; it is new
for every task - app/tools/scratch.py. Each helper's craft - how an expert works - is a short block in
its own prompt, the user's design: a small model follows a few fundamentals it reads every task more
reliably than it searches a guide. The prompts and the runner's nudges are in prompts.py.)

A worker finishes by calling submit_result (the analyst: save_report; the coder: save); its arguments
are checked by code before the main slot sees them: web sources must be pages the worker read (or at least found),
knowledge sources must be ones a tool returned, every number in a data answer must appear in a
tool result, and the coder's report must have every section and its code must have run (or its
report's facts say it fails). On a failed check the worker gets one chance to fix it; after that
the result goes back with the problem stated as a warning. The coder hands main its report, never
the code: the file itself goes to the UI as a card.
Charts, tables and images from worker tools go straight to the UI, not through the summary.

The coder sees pictures: what its script draws, an image it reads and an image the task names are put
into its own conversation as images (llama-server's vision part; at most IMAGES_SHOWN in view), so it
looks at them with everything it knows about the task - no separate vision call (view_image is main's).

The web, data and coder helpers can ask the user directly (ask_user, at most MAX_ASKS a task): the
card shows in the chat and the helper waits in place for the answer, keeping its work (main.Turn.ask).
A question inspect_data raises (are dates day first?) is asked by the runner itself, at once, and the
cleaning step of the option the user picks is applied by code - the model never relays or guesses it.

Besides its answer, the main slot gets a briefing to decide what to do next:
    suggestion         the worker's one sentence on what else could be looked into
    question_for_user  something only the user can settle (with options): ask it, then hand the
                       task back with the answer - a worker remembers nothing between tasks
    trail              written by code from the trace, only when the answer is incomplete or
                       flagged: which tools ran, what failed and why, calls that were not run
    warnings / note    failed checks / the step limit was reached
While it works, the worker is guided the same way: tool results say what to try next, repeated
calls are answered with what to do instead, and the last two steps say so.

The package: contracts.py (each helper defined once), runner.py (one task), evidence.py (the trace
and the guards before a call), checks.py (a result's checks), repairs.py (fields set right from the
trace). This module wires them to main: its delegate tools and its tool set."""

from __future__ import annotations

from collections.abc import Callable

from app.providers.base import Provider
from app.tools.core import ToolResult

from ..registry import TOOLS, Tool, ToolContext
from .checks import numbers_in, ungrounded_numbers  # noqa: F401 - used by main's checks
from .contracts import BRIEF, DELEGATE_OF, DELEGATES, MAIN_DIRECT, PROFILES, Delegate  # noqa: F401
from .evidence import Trace, _call_key  # noqa: F401 - Trace for tests, _call_key for main's checks
from .runner import _pictures, _submit_spec, _table_report, run_worker  # noqa: F401 - tests use them


def delegate_tools(worker: Provider, progress: Callable[[dict], None] | None = None) -> dict[str, Tool]:
    """The delegate tools for the main slot, bound to the worker-slot provider."""
    def make(name: str, d: Delegate) -> Tool:
        profile_name = d.profile

        def handler(ctx: ToolContext, args) -> ToolResult:
            task = getattr(args, "task", None) or args.question
            if getattr(args, "data", None):
                task += f"\n(The table to use: {args.data}.)"
            out = run_worker(worker, PROFILES[profile_name], task, ctx, progress=progress,
                             file=getattr(args, "file", None), table=getattr(args, "data", None))
            status = out.pop("status")
            if status != "ok":
                return ToolResult("failed", {"error": {"message": f"The {profile_name} helper failed: {out.get('error')}"}})
            return ToolResult("partial" if out.get("warnings") else "ok", out)
        return Tool(name, d.description, d.input_model, handler, labels=d.labels)
    return {name: make(name, d) for name, d in DELEGATES.items()}


def catalogue() -> dict[str, dict]:
    """How the UI names every tool the user may see run (GET /assistant/tools): what it is doing, what it did,
    per mode for files, and the helper's name for main's delegates."""
    out = {name: {"doing": t.labels[0], "done": t.labels[1],
                  **({"modes": {m: list(pair) for m, pair in t.mode_labels.items()}} if t.mode_labels else {})}
           for name, t in TOOLS.items()}
    out.update({name: {"doing": d.labels[0], "done": d.labels[1], "helper": d.helper} for name, d in DELEGATES.items()})
    return out


def main_tools(worker: Provider, ctx: ToolContext, **kwargs) -> dict[str, Tool]:
    """What the main slot sees: quick tools plus delegates (analyze_data only when there is a table)."""
    tools = {n: TOOLS[n] for n in MAIN_DIRECT}
    delegates = delegate_tools(worker, **kwargs)
    if not ctx.ws.ids("table"):
        delegates.pop("analyze_data")
    return {**tools, **delegates}
