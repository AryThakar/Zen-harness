"""calculate: exact arithmetic, so the model never does sums in its head.

A safe evaluator (simpleeval): numbers, + - * / // % ** ^, parentheses, pi and e, and FUNCTIONS -
no other names, attributes or calls. The set is what the model writes when left to itself
(bench/main/calc_probe.md: 20 everyday maths questions on the real model), in its own notation:
- n! and 12!/(3!*9!) for factorials (refused before: it handed the question to a helper);
- angles in degrees written 30°, 30 deg or 30 degrees (it wrote sin(30 degrees)). Trigonometry
  takes radians; a plain number in sin/cos/tan gets a note saying it was read as radians, because a
  bare tan(35) for "tan of 35 degrees" gave a wrong answer the model could not catch (with sin(30)
  it noticed -0.988 was not 0.5 and corrected itself);
- a $ is dropped (LaTeX's math marker: 'tan(35°)$' was refused);
- ln, log2 and log10 by those names (ln was refused before, and the model then confirmed a number
  it never computed);
- sinh, cosh, tanh and their inverses (sinh(2) was refused for a catenary cable's length, and the
  model worked the "accurate" answer out by hand).
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from simpleeval import InvalidExpression, SimpleEval

from .core import ToolError, ToolResult, ok, tool

MAX_FACTORIAL = 1000  # bigger arguments would hang the evaluator
MAX_DIGITS = 50  # a longer whole number is given in scientific notation, with its number of digits


def _factorial(n):
    if n != int(n) or not 0 <= n <= MAX_FACTORIAL:
        raise ValueError(f"factorial takes a whole number from 0 to {MAX_FACTORIAL}")
    return math.factorial(int(n))


FUNCTIONS = {
    "round": round, "abs": abs, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil,
    "sqrt": math.sqrt, "exp": math.exp, "ln": math.log, "log": math.log, "log2": math.log2, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "asinh": math.asinh, "acosh": math.acosh,
    "atanh": math.atanh, "factorial": _factorial,
}
_evaluator = SimpleEval(functions=FUNCTIONS, names={"pi": math.pi, "e": math.e})
DEGREES = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°|deg(?:rees?)?\b)", re.I)
# sin(30) - a plain number into a trig function: meant as degrees in every such call in the probe
# (tan(35) for "tan of 35 degrees" gave a wrong answer the model could not catch).
BARE_ANGLE = re.compile(r"\b(sin|cos|tan)\(\s*(-?\d+(?:\.\d+)?)\s*\)")


@tool
def calculate(expression: str) -> ToolResult:
    """Evaluate an arithmetic expression, e.g. '2400 * 0.15' or '(5240000 - 4810000) / 4810000 * 100'."""
    # 1,200,000 -> 1200000, but round(x, 2) keeps its comma: only a comma between a digit and exactly
    # three digits is a thousands separator (so write max(1, 234) with a space, as usual).
    # A $ is never arithmetic: it is LaTeX's math marker (seen: 'tan(35°)$' refused, and the model
    # then trusted its memory) or a currency sign.
    raw = str(expression).replace("$", "").replace("\\u00b0", "°")  # the degree sign also came as its escape
    expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", raw.strip()).replace("^", "**")
    if not expr or len(expr) > 500:
        raise ToolError("expression must be 1 to 500 characters.", field="expression")
    expr = _factorials(DEGREES.sub(r"(\1*pi/180)", expr))
    try:
        value = _evaluator.eval(expr)
    except ZeroDivisionError as exc:
        raise ToolError("Division by zero.", field="expression") from exc
    except (InvalidExpression, SyntaxError, TypeError, ValueError, OverflowError) as exc:
        known = f" Functions: {', '.join(FUNCTIONS)}; names: pi, e." if "not defined" in str(exc) else ""
        raise ToolError(f"Can't evaluate that expression: {str(exc).rstrip('.')}.{known}", field="expression") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float)
                                                                          and not math.isfinite(value)):
        raise ToolError("The expression must produce a finite number.", field="expression")
    result = {"expression": expr, "result": value if isinstance(value, int) else float(f"{value:.12g}")}
    if isinstance(value, int) and abs(value) >= 10 ** MAX_DIGITS:
        # A whole number too long to read (200! has 375 digits) - and too large for a float, which made the
        # finite check itself fail: its first 13 digits and its length, worked out exactly.
        shown = format(Decimal(value), ".12e")
        digits = int(shown.split("e+")[1]) + 1
        result.update(result=shown, digits=digits,
                      note=f"A whole number with {digits:,} digits, shown to 13 significant digits.")
    bare = BARE_ANGLE.findall(expr)
    if bare:  # the unit is said, not guessed: the model can only catch a wrong unit when it sees one
        result["note"] = (f"{', '.join(f'{f}({a})' for f, a in bare)} took the angle as radians. If degrees were "
                          f"meant, calculate again with {bare[0][1]}°.")
    return ok(result)


def _factorials(expr: str) -> str:
    """n! and (expression)! as factorial(...); != is left alone."""
    out = expr
    while (k := _bang(out)) >= 0:
        if out[k - 1] == ")":
            depth, j = 0, k - 1
            while j >= 0:
                depth += {")": 1, "(": -1}.get(out[j], 0)
                if depth == 0:
                    break
                j -= 1
            if j < 0:
                return expr  # unbalanced: let the evaluator say so
        else:
            j = k
            while j > 0 and (out[j - 1].isdigit() or out[j - 1] == "."):
                j -= 1
        out = f"{out[:j]}factorial({out[j:k]}){out[k + 1:]}"
    return out


def _bang(expr: str) -> int:
    """Where the first factorial '!' is (after a digit or ')', not part of !=), else -1."""
    for k, ch in enumerate(expr):
        if ch == "!" and k > 0 and (expr[k - 1].isdigit() or expr[k - 1] == ")") and not expr.startswith("!=", k):
            return k
    return -1
