"""draw_diagram: a Mermaid diagram (flowchart, sequence, class, state, ER, gantt, pie, mindmap,
timeline) drawn by the UI.

Before anything is drawn, two cheap checks catch the mistakes models make most: a missing or
unknown diagram type on the first line, and - in flowcharts - unbalanced brackets or quotes in a
node (an unclosed '[' breaks the whole diagram). Full syntax is checked by mermaid in the browser;
if it fails, the agent sends that error back for one retry, and after that the UI shows the code
instead of a broken drawing.
"""

from __future__ import annotations

import re

from .core import ToolError, ToolResult, ok, tool

MAX_CHARS = 20_000
TYPES = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram-v2", "stateDiagram", "erDiagram",
         "gantt", "pie", "mindmap", "timeline", "journey", "quadrantChart", "gitGraph")
PAIRS = {")": "(", "]": "[", "}": "{"}


@tool
def draw_diagram(mermaid: str, title: str | None = None) -> ToolResult:
    """Draw a diagram from Mermaid code."""
    code = _unfence(str(mermaid))
    if not code:
        raise ToolError("The diagram code is empty.", field="mermaid")
    if len(code) > MAX_CHARS:
        raise ToolError(f"The diagram is longer than {MAX_CHARS:,} characters; split it or simplify it.", field="mermaid")
    first = next((ln.strip() for ln in code.splitlines() if ln.strip() and not ln.strip().startswith("%%")), "")
    kind = next((t for t in TYPES if first == t or first.startswith(t + " ") or first.startswith(t + ";")), None)
    if kind is None:
        raise ToolError(f"The first line must name the diagram type ({', '.join(TYPES[:6])} ...), e.g. "
                        "'flowchart TD'.", field="mermaid")
    kind = "flowchart" if kind == "graph" else kind
    # Only flowcharts: in ER and class diagrams braces are relationship markers or span lines.
    problem = _unbalanced(code) if kind == "flowchart" else None
    if problem:
        raise ToolError(f"{problem} Fix it and send the whole diagram again.", field="mermaid")
    return ok({"diagram": "shown to the user", "kind": kind, "lines": code.count("\n") + 1},
              {"type": "diagram", "kind": kind, "title": title, "mermaid": code})


def _unfence(text: str) -> str:
    """Accept code wrapped in ```mermaid fences, as models often send it."""
    m = re.search(r"```(?:mermaid)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def _unbalanced(code: str) -> str | None:
    """The first unclosed or unexpected bracket, or an unclosed quote, by line (flowcharts)."""
    for n, line in enumerate(code.splitlines(), start=1):
        if line.strip().startswith("%%"):
            continue
        stack, quoted, label, prev = [], False, False, ""
        for ch in line:
            if ch == '"':
                quoted = not quoted
            elif quoted:
                continue
            elif ch == "|" and not stack:
                label = not label  # an edge label: -->|text| B
            elif label:
                continue
            elif ch == ">" and prev.isalnum() and not stack:
                stack.append("[")  # the asymmetric shape A>text] closes with ']'
            elif ch in "([{":
                stack.append(ch)
            elif ch in PAIRS:
                if not stack or stack[-1] != PAIRS[ch]:
                    return f"Line {n} has a '{ch}' without its opening bracket: {line.strip()!r}."
                stack.pop()
            prev = ch
        if quoted:
            return f"Line {n} has an unclosed '\"': {line.strip()!r}."
        if stack:
            return f"Line {n} has an unclosed '{stack[-1]}': {line.strip()!r}."
    return None
