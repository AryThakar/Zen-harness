"""calculate: exact arithmetic, so the model never does sums in its head.

A safe evaluator (simpleeval): numbers, + - * / // % **, parentheses and a few functions - no
names, attributes or calls beyond those. Same evaluator as the sales assistant's calculate tool.
"""

from __future__ import annotations

import math
import re

from simpleeval import InvalidExpression, SimpleEval

from .core import ToolError, ToolResult, ok, tool

_evaluator = SimpleEval(
    functions={"round": round, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "log": math.log,
               "log10": math.log10, "exp": math.exp, "floor": math.floor, "ceil": math.ceil},
    names={"pi": math.pi, "e": math.e},
)


@tool
def calculate(expression: str) -> ToolResult:
    """Evaluate an arithmetic expression, e.g. '2400 * 0.15' or '(5240000 - 4810000) / 4810000 * 100'."""
    # 1,200,000 -> 1200000, but round(x, 2) keeps its comma: only a comma between a digit and exactly
    # three digits is a thousands separator (so write max(1, 234) with a space, as usual).
    expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", str(expression).strip()).replace("^", "**")
    if not expr or len(expr) > 500:
        raise ToolError("expression must be 1 to 500 characters.", field="expression")
    try:
        value = _evaluator.eval(expr)
    except ZeroDivisionError as exc:
        raise ToolError("Division by zero.", field="expression") from exc
    except (InvalidExpression, SyntaxError, TypeError, ValueError, OverflowError) as exc:
        raise ToolError(f"Can't evaluate that expression: {exc}", field="expression") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToolError("The expression must produce a finite number.", field="expression")
    shown = value if isinstance(value, int) else float(f"{value:.12g}")
    return ok({"expression": expr, "result": shown})
