# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""compare: is a difference, correlation, association or A/B result real, or could it be chance?

The model says what kind of question it is; the code picks the test from the column types:

    difference   a number across groups   2 groups: Mann-Whitney U, effect = rank-biserial r
                                          3+ groups: Kruskal-Wallis, effect = epsilon squared
    correlation  two numbers              Spearman rho with a 95% range (Fisher z)
    association  two categories           chi-square, effect = Cramer's V (Fisher exact for
                                          a small 2 x 2 table)
    ab           2 variants and an        yes/no outcome: the two rates, their difference with a
                 outcome                  95% range (Newcombe's method from Wilson intervals) and a
                                          two-proportion z-test (Fisher exact when counts are small);
                                          a number: the two means, their difference with a 95%
                                          range and Welch's t-test (A/B metrics are averages)

Rank-based tests for the first three: no assumption of normal data, robust to outliers; their
verdict combines the p-value with the effect size, so 'significant but tiny' is said as such. An A/B
verdict states the difference and its range instead: a 1-point lift on a 5% rate is a small effect
size and a large result for a business. Ported from the analyst project (compare / correlate), where
these tests made no false discoveries on null data; the A/B test is new.
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
def compare(ws: Workspace, data: str, question: Literal["difference", "correlation", "association", "ab"],
            columns: list[str], alpha: float = 0.05) -> ToolResult:
    """Test whether a pattern between two columns is larger than chance would explain."""
    if len(columns) != 2:
        raise ToolError("Give exactly two columns.", field="columns")
    df = ws.table(data)
    a, b = (resolve_column(df, c, "columns") for c in columns)
    if a == b:
        raise ToolError("Give two different columns.", field="columns")
    if question == "ab":
        return _ab(ws, data, df, a, b, alpha)
    if question == "difference" and (pair := _rates_pair(df, a, b)):
        # A yes/no outcome across two groups: its difference is the difference of two rates - the A/B test
        # (seen: the model asked for 'difference' on variant + signed_up and rebuilt the test in a script).
        result = _ab(ws, data, df, *pair, alpha)
        result.result["note"] = "A yes/no outcome across 2 groups: compared as two rates (an A/B test)."
        return result
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
        raise ToolError("A difference needs one number column (the values) and one column of groups. For a yes/no "
                        "outcome across groups use question='association' (or 'ab' for 2 variants).", field="columns")
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
            "range": [number(lo), number(hi)], "confidence": "95%", "direction": direction,
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


# ---------------------------------------------------------------- A/B

POSITIVE = {"yes", "y", "true", "1", "1.0", "converted", "conversion", "success", "clicked", "click", "bought",
            "purchased", "won", "signed up", "subscribed", "retained", "active"}
CONTROL = {"a", "control", "baseline", "old", "original", "current", "existing", "before"}
Z95 = 1.959964


def _ab(ws: Workspace, data: str, df: pd.DataFrame, variant: str, outcome: str, alpha: float) -> ToolResult:
    frame = df[[variant, outcome]].dropna()
    groups = frame[variant].astype(str)
    names = sorted(groups.unique(), key=lambda g: (g.strip().lower() not in CONTROL, g))
    if len(names) != 2:
        raise ToolError(f"An A/B test compares 2 variants; '{variant}' has {len(names)} "
                        f"({', '.join(names[:6])}). Give the variant column first and the outcome second; for more "
                        "variants use question='association' (a yes/no outcome) or 'difference' (a number).",
                        field="columns")
    y = frame[outcome]
    binary = pd.api.types.is_bool_dtype(y) or y.astype(str).nunique() == 2
    if not binary and not _numeric(y):
        raise ToolError(f"'{outcome}' should be the outcome: yes/no (converted, clicked ...) or a number per row "
                        "(revenue, time). Give the variant column first and the outcome second.", field="columns")
    a_rows, b_rows = y[groups == names[0]], y[groups == names[1]]
    if min(len(a_rows), len(b_rows)) < MIN_GROUP:
        raise ToolError(f"Each variant needs at least {MIN_GROUP} rows ({names[0]}: {len(a_rows)}, {names[1]}: "
                        f"{len(b_rows)}).", invalid=False)
    res = _ab_rates(names, a_rows, b_rows, y) if binary else _ab_means(names, a_rows.astype(float),
                                                                       b_rows.astype(float))
    p = res["p_value"]
    d, (lo, hi) = res["difference"], res["difference_range"]
    unit = " points" if binary else ""
    if p < alpha:
        verdict = (f"{names[1]} differs from {names[0]} by more than chance would explain: {d:+g}{unit} "
                   f"(95% range {lo:+g} to {hi:+g}).")
    else:
        verdict = (f"Could be chance: {names[1]} vs {names[0]} {d:+g}{unit}; the 95% range {lo:+g} to {hi:+g} "
                   f"includes no difference, so these rows can't rule out a difference of up to "
                   f"{max(abs(lo), abs(hi)):g}{unit} either way.")
    res = {"question": "ab", "data": data, "variant_column": variant, "outcome_column": outcome,
           "control": names[0], "variant": names[1], **res, "verdict": verdict, "p_value": _p(p),
           "p_text": _p_text(p), "alpha": alpha}
    warning = ws.issues_warning(data)
    if warning:
        res["data_warning"] = warning
    measure = "rate" if binary else "mean"
    return ok(res, {"type": "table", "title": f"A/B test: {outcome} by {variant}",
                    "columns": ["variant", "rows", "conversion rate" if binary else f"mean {outcome}"],
                    "rows": [[g["variant"], g["rows"], g[measure]] for g in res["groups"]], "notes": [verdict]})


def _rates_pair(df: pd.DataFrame, a: str, b: str) -> tuple[str, str] | None:
    """(groups, outcome) when one column has 2 groups and the other is a yes/no outcome, else None."""
    def two(c):
        return df[c].dropna().astype(str).nunique() == 2
    def number(c):
        return _numeric(df[c]) and df[c].nunique() > 2
    if two(a) and two(b) and not number(a) and not number(b):
        outcome = b if _positive_known(df[b]) or not _positive_known(df[a]) else a
        return (a if outcome == b else b), outcome
    return None


def _positive_known(s: pd.Series) -> bool:
    return pd.api.types.is_bool_dtype(s) or any(str(v).strip().lower() in POSITIVE for v in s.dropna().unique())


def _positive(y: pd.Series):
    """The outcome value that counts as a success: True, 1, 'yes', 'converted' ... else the rarer one."""
    if pd.api.types.is_bool_dtype(y):
        return True
    for v in pd.unique(y):
        if str(v).strip().lower() in POSITIVE:
            return v
    return y.value_counts().idxmin()


def _wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval of a proportion."""
    p = k / n
    centre = (p + Z95 ** 2 / (2 * n)) / (1 + Z95 ** 2 / n)
    half = Z95 * np.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / (1 + Z95 ** 2 / n)
    return centre - half, centre + half


def _ab_rates(names: list[str], a: pd.Series, b: pd.Series, y: pd.Series) -> dict:
    hit = _positive(y)
    ka, kb, na, nb = int((a == hit).sum()), int((b == hit).sum()), len(a), len(b)
    pa, pb = ka / na, kb / nb
    (la, ua), (lb, ub) = _wilson(ka, na), _wilson(kb, nb)
    d = pb - pa
    lo = d - np.sqrt((pb - lb) ** 2 + (ua - pa) ** 2)  # Newcombe's hybrid score interval
    hi = d + np.sqrt((ub - pb) ** 2 + (pa - la) ** 2)
    table = np.array([[kb, nb - kb], [ka, na - ka]])
    expected = table.sum(1, keepdims=True) * table.sum(0, keepdims=True) / table.sum()
    if (expected < 5).any():
        p, test = float(stats.fisher_exact(table)[1]), "Fisher exact test"
    else:
        pooled = (ka + kb) / (na + nb)
        se = np.sqrt(pooled * (1 - pooled) * (1 / na + 1 / nb))
        p, test = (float(2 * stats.norm.sf(abs(d) / se)) if se > 0 else 1.0), "two-proportion z-test"
    res = {"test": test, "outcome_counted": str(hit),
           "groups": [{"variant": names[0], "rows": na, "successes": ka, "rate": number(pa)},
                      {"variant": names[1], "rows": nb, "successes": kb, "rate": number(pb)}],
           "difference": number(100 * d, 2), "difference_range": [number(100 * lo, 2), number(100 * hi, 2)],
           "difference_unit": "percentage points (variant rate - control rate)", "p_value": p}
    if pa > 0:
        res["relative_change"] = number(d / pa, 3)
    return res


def _ab_means(names: list[str], a: pd.Series, b: pd.Series) -> dict:
    t = stats.ttest_ind(b, a, equal_var=False)
    ci = t.confidence_interval(0.95)
    d = float(b.mean() - a.mean())
    res = {"test": "Welch's t-test",
           "groups": [{"variant": n, "rows": len(x), "mean": number(float(x.mean())), "median": number(float(x.median()))}
                      for n, x in zip(names, (a, b))],
           "difference": number(d), "difference_range": [number(float(ci.low)), number(float(ci.high))],
           "difference_unit": "variant mean - control mean", "p_value": float(t.pvalue)}
    if a.mean():
        res["relative_change"] = number(d / abs(float(a.mean())), 3)
    if max(a.skew(), b.skew()) > 2:
        res["note"] = "The values are very skewed: a few large ones move the means; compare the medians too."
    return res
