# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""draw_diagram: a Mermaid diagram (flowchart, sequence, class, state, ER, gantt, pie, mindmap,
timeline) drawn by the UI.

The code is checked and mended before it is drawn, for the mistakes small models make most (all
seen with Mermaid 11):
- a missing or unknown diagram type on the first line: refused, with how to start;
- in flowcharts, unbalanced brackets or quotes in a node: refused, with the line;
- in flowcharts, brackets or ';' inside a node's label, e.g. C[Hold: Q(next) = Q(current)]: Mermaid
  reads them as the label's end. Such labels are quoted - C["Hold: Q(next) = Q(current)"] - which is
  Mermaid's own way to write any text in a label;
- in sequence diagrams, deactivating a participant that is not active (a hard error in Mermaid):
  the stray deactivation is dropped.
Anything else Mermaid rejects is shown by the UI as its error with the code.
"""

from __future__ import annotations

import re

from .core import ToolError, ToolResult, ok, tool

MAX_CHARS = 20_000
TYPES = ("flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram-v2", "stateDiagram", "erDiagram",
         "gantt", "pie", "mindmap", "timeline", "journey", "quadrantChart", "gitGraph")
PAIRS = {")": "(", "]": "[", "}": "{"}
# Node shapes, longest opener first: [[sub]] [(db)] ([stadium]) ((circle)) {{hex}} [box] (round) {rhombus} >flag]
SHAPES = (("[[", "]]"), ("[(", ")]"), ("([", "])"), ("((", "))"), ("{{", "}}"), ("[", "]"), ("(", ")"), ("{", "}"),
          (">", "]"))
NODE = re.compile(r"(?<![\w\-.|])([A-Za-z_][\w-]*)(?=[\[({>])")
NOT_NODES = ("%%", "classDef", "class ", "style ", "linkStyle", "click ")
SPECIAL = set("()[]{};")
EDGE_LABEL = re.compile(r'\|(?!")([^|"]*[()\[\]{}][^|"]*)\|')


@tool
def draw_diagram(mermaid: str, title: str | None = None) -> ToolResult:
    """Draw a diagram from Mermaid code."""
    kind, code = mend(_unfence(str(mermaid)))
    return ok({"diagram": "shown to the user", "kind": kind, "lines": code.count("\n") + 1},
              {"type": "diagram", "kind": kind, "title": title, "mermaid": code})


def mend(code: str) -> tuple[str, str]:
    """(the diagram's kind, its code mended); ToolError for what can't be mended."""
    if not code.strip():
        raise ToolError("The diagram code is empty.", field="mermaid")
    if len(code) > MAX_CHARS:
        raise ToolError(f"The diagram is longer than {MAX_CHARS:,} characters; split it or simplify it.", field="mermaid")
    first = next((ln.strip() for ln in code.splitlines() if ln.strip() and not ln.strip().startswith("%%")), "")
    kind = next((t for t in TYPES if first == t or first.startswith(t + " ") or first.startswith(t + ";")), None)
    if kind is None:
        raise ToolError(f"The first line must name the diagram type ({', '.join(TYPES[:6])} ...), e.g. "
                        "'flowchart TD'.", field="mermaid")
    kind = "flowchart" if kind == "graph" else kind
    if kind == "flowchart":  # braces mean other things in ER and class diagrams
        problem = _unbalanced(code)
        if problem:
            raise ToolError(f"{problem} Fix it and send the whole diagram again.", field="mermaid")
        code = "\n".join(ln if ln.strip().startswith(NOT_NODES) else _quote_labels(_drop_comment(ln))
                         for ln in code.splitlines())
    elif kind == "sequenceDiagram":
        code = _balance_activations(code)
    return kind, code


FENCED = re.compile(r"```mermaid[^\S\n]*\n(.*?)```", re.S)


def mend_blocks(text: str) -> str:
    """A reply's ```mermaid blocks mended the same way (a model sometimes writes the diagram into its
    answer instead of calling draw_diagram; the UI draws such blocks). One that can't be mended is
    left as it is."""
    def one(m: re.Match) -> str:
        try:
            return "```mermaid\n" + mend(m.group(1).strip())[1] + "\n```"
        except ToolError:
            return m.group(0)
    return FENCED.sub(one, text)


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


def _drop_comment(line: str) -> str:
    """A comment written after a statement (A --> B; % note, A --> B // note) or a line commented
    with // or #: Mermaid only knows %% on a line of its own. Labels and quotes are left alone."""
    stripped = line.lstrip()
    if stripped.startswith(("//", "#")):
        return line[:len(line) - len(stripped)] + "%% " + stripped.lstrip("/#").strip()
    depth, quoted, piped = 0, False, False
    for k, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "|" and depth == 0:
            piped = not piped
        elif piped:
            continue
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and (line.startswith("//", k) or ch == "%") and (k == 0 or line[k - 1] in " \t;"):
            return line[:k].rstrip()
    return line


def _quote_labels(line: str) -> str:
    """Quote each node label on the line that holds brackets or ';' (already balanced), and each
    edge label between pipes that holds brackets (-->|J=1 (set)| fails; ';' there is fine)."""
    line = EDGE_LABEL.sub(lambda e: '|"' + e.group(1).replace('"', "#quot;") + '"|', line)
    out, i = [], 0
    while (m := NODE.search(line, i)) is not None:
        if line.count('"', 0, m.start()) % 2:  # inside quoted text
            out.append(line[i:m.end()])
            i = m.end()
            continue
        start = m.end()
        opener, closer = next(((o, c) for o, c in SHAPES if line.startswith(o, start)), (None, None))
        end = _closing(line, start + len(opener), closer) if opener else -1
        if end < 0:
            out.append(line[i:start])
            i = start
            continue
        label = line[start + len(opener):end]
        if not (label.startswith('"') and label.endswith('"')) and SPECIAL & set(label):
            label = '"' + label.replace('"', "#quot;") + '"'
        out.append(line[i:start] + opener + label + closer)
        i = end + len(closer)
    return "".join(out) + line[i:]


def _closing(line: str, start: int, closer: str) -> int:
    """Where the label that starts at ``start`` ends: ``closer`` outside nested brackets and quotes."""
    depth, quoted = 0, False
    for k in range(start, len(line)):
        ch = line[k]
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif depth == 0 and line.startswith(closer, k):
            return k
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    return -1


# A->>+B: msg activates B (the receiver); B-->>-A: msg deactivates B (the sender) - Mermaid's shorthand
ARROW = re.compile(r"^\s*([^\s:+-]+)\s*--?(?:>>|>|x|\))([+-])?\s*([^:\s]+)\s*:")


def _balance_activations(code: str) -> str:
    """Drop deactivations of participants that are not active (Mermaid stops on them)."""
    active: dict[str, int] = {}
    out = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("activate "):
            name = stripped.split(None, 1)[1]
            active[name] = active.get(name, 0) + 1
        elif stripped.startswith("deactivate "):
            name = stripped.split(None, 1)[1]
            if not active.get(name):
                continue
            active[name] -= 1
        elif (m := ARROW.match(line)) and m.group(2):
            if m.group(2) == "+":
                active[m.group(3)] = active.get(m.group(3), 0) + 1
            elif active.get(m.group(1)):
                active[m.group(1)] -= 1
            else:
                line = line[:m.start(2)] + line[m.end(2):]  # '-' on a sender that is not active
        out.append(line)
    return "\n".join(out)
