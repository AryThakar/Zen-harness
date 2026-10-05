"""ask_user: a question for the user, shown in the UI with option buttons, a text box and Skip.

The tool only builds the question; the agent pauses until the user answers, then the model gets
answer_result(): the choice, the typed text, or a skip (with the instruction to go on with a
stated default). Questions from inspect_data arrive with options that each carry the clean_data
step to run, so the user's click decides the step without the model in between.

Use it only when reasonable readings give materially different results - never for preferences
that don't change the answer.
"""

from __future__ import annotations

from .core import ToolError, ToolResult, ok, tool

MAX_QUESTION = 500
MAX_OPTIONS = 6
MAX_OPTION = 120


@tool
def ask_user(question: str, options: list[str] | None = None, allow_text: bool = True) -> ToolResult:
    """Ask the user one question; the answer comes back as this tool's result."""
    question = " ".join(str(question).split())
    if not question:
        raise ToolError("question is empty.", field="question")
    if len(question) > MAX_QUESTION:
        raise ToolError(f"question is longer than {MAX_QUESTION} characters; ask one short question.", field="question")
    options = [" ".join(str(o).split()) for o in options or []]
    if len(options) > MAX_OPTIONS:
        raise ToolError(f"At most {MAX_OPTIONS} options.", field="options")
    if any(not o or len(o) > MAX_OPTION for o in options) or len(set(options)) < len(options):
        raise ToolError(f"Options must be distinct and 1 to {MAX_OPTION} characters.", field="options")
    if not options and not allow_text:
        raise ToolError("Give options or allow a typed answer.", field="options")
    return ok({"asked": question}, {"type": "question", "question": question, "options": options,
                                    "allow_text": allow_text, "allow_skip": True})


def answer_result(question: str, answer: str | None) -> dict:
    """What the model receives once the user has answered (None = skipped)."""
    if answer is None or not answer.strip():
        return {"question": question, "skipped": True,
                "note": "The user skipped the question: they want you to decide. Take the most likely meaning, "
                        "say in a few words which one you took, and answer it now (with tools if needed) - "
                        "don't ask again and don't refuse."}
    return {"question": question, "answer": answer.strip()}
