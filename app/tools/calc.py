# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""calculate: exact maths, so the model never works numbers out in its head - arithmetic at once, and the rest
(equations, derivatives, integrals, limits, sums, matrices) by SymPy.

Arithmetic first, by the safe evaluator below. What it can't read - a symbol (x), a SymPy function (solve, diff,
integrate, limit, Matrix ...), an equation written 'lhs = rhs' - goes to SymPy, in a sandbox process of its own (an
empty folder, no network, a time limit: SymPy reads an expression by evaluating it). Measured before: calculate
was arithmetic only, so every real maths problem went to the analyst-programmer - 10 since 3 Oct, the calculus
problem of 10 Oct for 8 minutes - or was worked out in the model's head.

The arithmetic evaluator:

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

from simpleeval import FunctionNotDefined, InvalidExpression, NameNotDefined, SimpleEval

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
    parts = _top_level(expr)
    if len(parts) > 1 and not EQUATION.search(expr):  # several at once: each worked out (measured 2026-10-17: ten
        # ratios as one expression - "Tuple is not available" - and main worked them out in its head instead)
        if len(parts) > MAX_PARTS:
            raise ToolError(f"At most {MAX_PARTS} expressions at once.", field="expression")
        done = [calculate(p) for p in parts]
        for p, d in zip(parts, done):
            if d.status != "ok":
                raise ToolError(f"In '{p}': {d.result.get('error', {}).get('message', 'it failed')}", field="expression")
        return ok({"expressions": parts, "results": [d.result["result"] for d in done]})
    if not expr or len(expr) > 500:
        raise ToolError("expression must be 1 to 500 characters.", field="expression")
    expr = _factorials(DEGREES.sub(r"(\1*pi/180)", expr))
    if EQUATION.search(expr):  # 'x^2 + 1 = 10': an equation, solved for its symbols
        return symbolic(expr)
    try:
        value = _evaluator.eval(expr)
    except (NameNotDefined, FunctionNotDefined):  # a symbol or a SymPy function: maths beyond arithmetic
        return symbolic(expr)
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


MAX_PARTS = 20


def _top_level(expr: str) -> list[str]:
    """The expression split at its top-level commas ('a / b, c / d'), not those inside brackets (round(x, 2))."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(expr):
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(expr[start:i].strip())
            start = i + 1
    parts.append(expr[start:].strip())
    return [p for p in parts if p]


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


# ---------------------------------------------------------------- SymPy

EQUATION = re.compile(r"(?<![=<>!])=(?!=)")  # one '=' alone: an equation, not == <= >= !=
SYMBOLIC_TIMEOUT = 20
SYMPY = r"""  # raw: its regex is written as the script will read it
import json
import sympy as sp
from sympy.parsing.sympy_parser import convert_xor, implicit_multiplication, parse_expr, standard_transformations
names = {{k: getattr(sp, k) for k in dir(sp) if not k.startswith("_")}}
names.update(ln=sp.log, e=sp.E, inf=sp.oo, Max=sp.Max, Min=sp.Min)
names.update({{s: sp.Symbol(s) for s in ("x", "y", "z", "t", "n", "k", "a", "b", "c", "u", "v", "w")}})
read = lambda s: parse_expr(s, local_dict=names,
                            transformations=standard_transformations + (implicit_multiplication, convert_xor))
text = {expr!r}
import re
unknown = [f for f in re.findall(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", text) if f not in names]
if unknown:  # an unknown name before '(' would be read as a symbol times the bracket: said instead
    raise NameError("no function " + ", ".join(dict.fromkeys(unknown)) + " in SymPy")
if {equation!r}:
    left, right = text.split("=", 1)
    equation = sp.Eq(read(left), read(right))
    value = sp.solve(equation, sorted(equation.free_symbols, key=str) or None, dict=True)
else:
    value = read(text)
def number(v):
    try:
        if getattr(v, "is_number", False):
            n = complex(sp.N(v, 15))
            return n.real if abs(n.imag) < 1e-12 else str(sp.N(v, 12))
    except (TypeError, ValueError):
        pass
    return None
if isinstance(value, (list, tuple, set)):
    items = list(value)
    numeric = [{{str(k): number(x) for k, x in i.items()}} if isinstance(i, dict) else number(i) for i in items]
else:
    numeric = number(value)
print(json.dumps({{"result": sp.sstr(value), "value": numeric}}))
"""


def symbolic(expr: str) -> ToolResult:
    """An expression or equation worked out by SymPy, exactly - and as a number where it is one."""
    import json
    import tempfile
    from pathlib import Path

    from .sandbox import run_python
    from .workspace import Workspace
    equation = bool(EQUATION.search(expr))
    with tempfile.TemporaryDirectory() as tmp:  # an empty workspace: the maths can reach nothing of the user's
        ran = run_python(Workspace(Path(tmp) / "maths"), SYMPY.format(expr=expr, equation=equation),
                         SYMBOLIC_TIMEOUT, name="calculate.py")
    if ran.status != "ok":
        if "time limit" in str((ran.result.get("error") or {}).get("message", "")):
            raise ToolError(f"That took longer than {SYMBOLIC_TIMEOUT} s. For a definite integral's number, try "
                            "Integral(f, (x, a, b)).evalf(); for an equation's roots, nsolve(f, x, guess).",
                            field="expression")
        last = next((ln for ln in reversed(ran.result.get("errors", "").splitlines()) if ln.strip()), "an error")
        raise ToolError(f"Can't work that out: {last.strip()[:200]}. Write it in SymPy's notation - x, y, z, t, n "
                        "are symbols: solve(x**2 - 4, x), x**2 + 1 = 10, diff(x*sin(x), x), "
                        "integrate(exp(-x**2), (x, 0, oo)), limit(sin(x)/x, x, 0), Matrix([[1, 2], [3, 4]]).det().",
                        field="expression")
    out = json.loads(ran.result["output"].strip().splitlines()[-1])
    result = {"expression": expr, "result": out["result"]}
    if out.get("value") is not None and str(out["value"]) != out["result"]:
        result["value"] = out["value"]
    return ok(result)
