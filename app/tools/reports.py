"""The report kept on a file (docs/ARCHITECTURE.md, section 5): its sections, in a fixed order, then facts the app
writes itself - so a report can't claim what didn't happen. The first line is the file's line in lists. Used by the
report kept on a table at the analyst-programmer's finish (app/assistant/workers/runner.py); a program's report is
written by its runs (app/tools/run.py).
"""

from __future__ import annotations

DATA_SECTIONS = (("question", ""), ("results", "Results"), ("caveats", "Caveats and next steps"))
MAX_REPORT_WORDS = 450  # a report must fit beside the work in a 16K worker context


def section_problems(layout: tuple, sections: dict) -> list[str]:
    """Empty sections, and a report too long to read beside the work."""
    problems = [f"the report's {key} is empty" for key, _ in layout if not str(sections.get(key) or "").strip()]
    words = sum(len(str(sections.get(key) or "").split()) for key, _ in layout)
    if words > MAX_REPORT_WORDS:
        problems.append(f"the report is {words} words; keep it under {MAX_REPORT_WORDS}")
    return problems


def assemble(layout: tuple, sections: dict, facts: list[str]) -> str:
    """The report: the first section as its title line, the rest under headings, then the app's facts."""
    first = str(sections.get(layout[0][0]) or "").strip()
    head, _, rest = first.partition("\n")
    parts = [f"# {head.strip().rstrip('.')}"] + ([rest.strip()] if rest.strip() else [])
    for key, title in layout[1:]:
        parts.append(f"## {title}\n{str(sections.get(key) or '').strip()}")
    parts.append("## Facts (written by the app)\n" + "\n".join(f"- {f}" for f in facts))
    return "\n\n".join(parts) + "\n"
