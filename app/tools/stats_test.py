"""stats_test: is a difference, correlation or association real, or could it be chance?

The model says what kind of question it is; the code picks the test from the column types:

    difference   a number across groups   2 groups: Mann-Whitney U, effect = rank-biserial r
                                          3+ groups: Kruskal-Wallis, effect = epsilon squared
    correlation  two numbers              Spearman rho with a 95% range (Fisher z)
    association  two categories           chi-square, effect = Cramer's V (Fisher exact for
                                          a small 2 x 2 table)

Rank-based tests: no assumption of normal data, robust to outliers. Every verdict combines the
p-value with the effect size, so 'significant but tiny' is said as such. Ported from the analyst
project (compare / correlate), where these tests made no false discoveries on null data.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from .core import ToolError, ToolResult, number, ok, tool
from .workspace import Workspace, resolve_column

MIN_GROUP = 5
MAX_GROUPS = 20
# Conventional cut-offs for small / medium / large effects.
CUTS = {"rank_biserial": (0.1, 0.3, 0.5), "epsilon_squared": (0.01, 0.06, 0.14),
        "spearman": (0.1, 0.3, 0.5), "cramers_v": (0.1, 0.3, 0.5)}


@tool
def stats_test(ws: Workspace, data: str, question: Literal["difference", "correlation", "association"],
               columns: list[str], alpha: float = 0.05) -> ToolResult:
    """Test whether a pattern between two columns is larger than chance would explain."""
    if len(columns) != 2:
        raise ToolError("Give exactly two columns.", field="columns")
    df = ws.table(data)
    a, b = (resolve_column(df, c, "columns") for c in columns)
    if a == b:
        raise ToolError("Give two different columns.", field="columns")
    if question == "difference":
        res = _difference(df, a, b)
    elif question == "correlation":
        res = _correlation(df, a, b)
    else:
        res = _association(df, a, b)
    p, size = res["p_value"], res["effect_label"]
    if p >= alpha:
        verdict = "could be chance (not statistically significant)"
    elif size == "negligible":
        verdict = "is statistically significant but negligible in size"
    else:
        verdict = f"is larger than chance would explain ({size} effect)"
    res["verdict"] = f"The {question} {verdict}."
    res = {"question": question, "data": data, **res, "p_value": _p(p), "p_text": _p_text(p), "alpha": alpha}
    warning = ws.issues_warning(data)
    if warning:
        res["data_warning"] = warning
    return ok(res, {"type": "table", "title": f"{res['test']}: {a} and {b}",
                    "columns": list(res.get("groups", [{}])[0].keys()) if res.get("groups") else ["result"],
                    "rows": [list(g.values()) for g in res.get("groups", [])] or [[res["verdict"]]]})


def _numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _effect(value: float, kind: str) -> str:
    lo, mid, hi = CUTS[kind]
    v = abs(value)
    return "negligible" if v < lo else "small" if v < mid else "medium" if v < hi else "large"


def _p(p: float) -> float:
    return float(f"{p:.3g}") if p >= 1e-4 else float(f"{p:.1e}")


def _p_text(p: float) -> str:
    """How to quote it: a p-value of 0.0 is only too small for floating point, not zero."""
    return "p < 0.0001" if p < 1e-4 else f"p = {p:.2g}" if p < 0.01 else f"p = {p:.2f}"


def _difference(df, a, b):
    # The number is the column with numbers; the other one holds the groups.
    if _numeric(df[a]) and not _numeric(df[b]):
        value, group = a, b
    elif _numeric(df[b]) and not _numeric(df[a]):
        value, group = b, a
    elif _numeric(df[a]) and _numeric(df[b]):
        few = [c for c in (a, b) if df[c].nunique() <= 10]
        if len(few) != 1:
            raise ToolError("Both columns are numbers; for how two numbers move together use question='correlation'.",
                            field="question")
        group, value = few[0], b if few[0] == a else a
    else:
        raise ToolError("A difference needs one number column (the values) and one column of groups.",
                        field="columns")
    frame = df[[group, value]].dropna()
    frame = frame.assign(_g=frame[group].astype(str))
    sizes = frame["_g"].value_counts()
    keep = sizes[sizes >= MIN_GROUP].head(MAX_GROUPS).index
    left_out = [g for g in sizes.index if g not in keep]
    frame = frame[frame["_g"].isin(keep)]
    if len(keep) < 2:
        raise ToolError(f"Fewer than two groups of {group} have at least {MIN_GROUP} rows; nothing to test.",
                        invalid=False)
    samples = {g: frame.loc[frame["_g"] == g, value].astype(float).to_numpy() for g in keep}
    groups = [{"group": g, "rows": len(v), "median": number(float(np.median(v))), "mean": number(float(v.mean()))}
              for g, v in samples.items()]
    groups.sort(key=lambda r: r["median"], reverse=True)
    if len(keep) == 2:
        (g1, x1), (g2, x2) = samples.items()
        u, p = stats.mannwhitneyu(x1, x2, alternative="two-sided")
        r = 2 * u / (len(x1) * len(x2)) - 1  # rank-biserial: P(x1 > x2) - P(x1 < x2)
        effect, kind, test = abs(r), "rank_biserial", "Mann-Whitney U"
        higher = g1 if r > 0 else g2
    else:
        h, p = stats.kruskal(*samples.values())
        effect, kind, test = max(0.0, h / (len(frame) - 1)), "epsilon_squared", "Kruskal-Wallis"
    res = {"test": test, "value_column": value, "group_column": group, "rows": len(frame),
           "effect_size": number(float(effect)), "effect_measure": kind, "effect_label": _effect(effect, kind),
           "p_value": float(p), "groups": groups}
    if len(keep) == 2:
        res["tends_higher"] = higher
    if left_out:
        res["note"] = f"Left out {len(left_out)} groups with fewer than {MIN_GROUP} rows or beyond the largest {MAX_GROUPS}."
    return res


def _correlation(df, a, b):
    if not (_numeric(df[a]) and _numeric(df[b])):
        raise ToolError("A correlation needs two number columns; for two categories use question='association'.",
                        field="question")
    both = df[[a, b]].dropna().astype(float)
    n = len(both)
    if n < 10:
        raise ToolError(f"Only {n} rows have both values; at least 10 are needed.", invalid=False)
    if both[a].nunique() < 2 or both[b].nunique() < 2:
        raise ToolError("One of the columns has a single value; there is nothing to correlate.", invalid=False)
    rho, p = stats.spearmanr(both[a], both[b])
    lo, hi = _spearman_range(float(rho), n)
    direction = "rise together" if rho > 0 else "move in opposite directions"
    return {"test": "Spearman rank correlation", "rows": n, "rho": number(float(rho)),
            "range_95": [number(lo), number(hi)], "direction": direction,
            "effect_size": number(float(rho)), "effect_measure": "spearman", "effect_label": _effect(rho, "spearman"),
            "p_value": float(p), "note": "A correlation is not a cause."}


def _spearman_range(rho: float, n: int) -> tuple[float, float]:
    """95% range: Fisher transform with the Fieller-Hartley-Pearson standard error sqrt(1.06 / (n - 3))."""
    if n <= 3:
        return float("nan"), float("nan")
    z = np.arctanh(np.clip(rho, -0.999999, 0.999999))
    se = np.sqrt(1.06 / (n - 3))
    return float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se))


def _association(df, a, b):
    for c in (a, b):
        if _numeric(df[c]) and df[c].nunique() > 10:
            raise ToolError(f"'{c}' is a number column with many values; an association needs two category "
                            "columns (for a number across groups use question='difference').", field="question")
    frame = df[[a, b]].dropna().astype(str)
    table = pd.crosstab(frame[a], frame[b])
    table = table.loc[table.sum(axis=1).nlargest(MAX_GROUPS).index, table.sum().nlargest(MAX_GROUPS).index]
    if table.shape[0] < 2 or table.shape[1] < 2:
        raise ToolError("Each column needs at least two values to test an association.", invalid=False)
    n = int(table.to_numpy().sum())
    chi2, p, dof, expected = stats.chi2_contingency(table, correction=False)
    v = float(np.sqrt(chi2 / (n * (min(table.shape) - 1))))
    test = "chi-square test of independence"
    small = float((expected < 5).mean())
    note = None
    if table.shape == (2, 2) and small > 0:
        p = float(stats.fisher_exact(table.to_numpy())[1])
        test = "Fisher exact test"
    elif small > 0.2:
        note = f"{small:.0%} of the cells expect fewer than 5 rows; the p-value is approximate."
    res = {"test": test, "rows": n, "table_shape": list(table.shape), "effect_size": number(v),
           "effect_measure": "cramers_v", "effect_label": _effect(v, "cramers_v"), "p_value": float(p)}
    # Where the association shows: the cells furthest above what independence would give.
    resid = (table - expected) / np.sqrt(expected)
    cells = resid.stack().sort_values(ascending=False).head(3)
    res["most_over_represented"] = [{a: str(i), b: str(j), "rows": int(table.loc[i, j]),
                                     "expected": number(float(expected[table.index.get_loc(i), table.columns.get_loc(j)]), 1)}
                                    for (i, j), _ in cells.items()]
    if note:
        res["note"] = note
    return res
