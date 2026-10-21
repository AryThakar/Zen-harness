# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""find_patterns: groups of similar things, unusual values, and items that occur together.

    segments  group similar customers (or products, places, rows) by their numbers - k-means on
              standardised, log-scaled numbers, the number of groups chosen by silhouette (the fewest
              within 10% of the best separation) - and describe each group by what sets it apart.
              With an entity (customer ...) each one is described by its rows: how many orders, how
              much it spent, days since it was last seen and how long it has been active.
    unusual   with a date: periods far from what the trend and the season predict (robust z-score of
              the gap to a rolling-median trend plus the seasonal median), or extreme against the
              whole history; consecutive unusual periods count as one event. Without a date: rows
              whose numbers are extreme (robust z-score on a log scale for skewed values).
    together  items in the same order (visit, case ...) more often than chance: lift, and a
              one-sided Fisher test per pair with false-discovery control across all the pairs tested.

Every result says how clear the pattern is (separation, how far from normal, how sure) and saves a
table the other tools can chart or query (each thing's group, the unusual rows, the pairs).
Ported from the analyst project (analyst/ops/learn.py segments / together, timeseries.py anomalies);
the row check uses robust z-scores instead of ECOD so each flag names its column.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from .chart_image import save_chart
from .core import ToolError, ToolResult, number, ok, tool
from .forecast import MAX_GROUPS, SEASON, _positions, _series
from .periods import CODE, ORDER, WORD, native_freq, split_dates, split_time
from .roles import table_roles
from .workspace import Workspace, resolve_column

MIN_ITEMS = 20
MAX_K = 6
FEATURES = 8
THRESHOLD = 5.0  # robust z beyond which a value is unusual (real data has heavier tails than a bell curve)
MAX_BASKET = 50
TOP_ITEMS = 500
MAX_PERIODS = 800  # unusual periods: the data's own grain, coarsened beyond this many


@tool
def find_patterns(ws: Workspace, data: str, kind: Literal["segments", "unusual", "together"],
                  group: str | None = None, columns: list[str] | None = None, value: str | None = None,
                  by: str | None = None, basket: str | None = None, item: str | None = None,
                  time_unit: Literal["auto", "day", "week", "month", "quarter", "year"] = "auto") -> ToolResult:
    """Find segments, unusual values or items that occur together."""
    df = ws.table(data)
    roles = table_roles(ws, data, df)
    cols = [resolve_column(df, c, "columns") for c in columns] if columns else None
    warning = ws.issues_warning(data)
    if kind == "segments":
        result, artifact = _segments(ws, data, df, roles, resolve_column(df, group, "group") if group else None, cols)
    elif kind == "unusual":
        value = resolve_column(df, value, "value") if value else None
        result, artifact = _unusual(ws, data, df, roles, value, resolve_column(df, by, "by") if by else None, cols,
                                    time_unit)
    else:
        result, artifact = _together(ws, data, df, roles, resolve_column(df, basket, "basket") if basket else None,
                                     resolve_column(df, item, "item") if item else None)
    if warning:
        result["data_warning"] = warning
    if artifact.get("type") == "chart":  # unusual periods' chart, kept as its chart file
        artifact["id"] = result["chart_file"] = save_chart(ws, artifact, parent=data)
    return ok({"kind": kind, "data": data, **result}, artifact)


def _numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _robust_z(x: pd.Series) -> pd.Series:
    med = x.median()
    mad = (x - med).abs().median() * 1.4826
    # Floored at a quarter of the standard deviation: in spiky data with many quiet values the MAD is
    # tiny and every busy value would look extreme.
    scale = max(mad, 0.25 * (x.std() or 0)) or 1.0
    return (x - med) / scale


def _log_if_skewed(x: pd.Series) -> pd.Series:
    return np.log1p(x) if (x.dropna() >= 0).all() and x.skew() > 1 else x


# ---------------------------------------------------------------- segments

def _segments(ws, data, df, roles, entity, columns):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    entity = entity or roles.get("entity")
    table, noun, how = _profile_table(df, roles, entity, columns)
    if len(table) < MIN_ITEMS:
        raise ToolError(f"Only {len(table)} {noun}; grouping needs at least {MIN_ITEMS}.", invalid=False)
    x = table.astype(float).apply(lambda c: _log_if_skewed(c.fillna(c.median())))
    x = x.loc[:, x.std() > 0]
    if x.shape[1] == 0:
        raise ToolError("The numbers are the same for every one; there is nothing to group by.", invalid=False)
    z = ((x - x.mean()) / x.std()).to_numpy()
    sample = np.random.default_rng(0).choice(len(z), min(len(z), 3000), replace=False)
    tried = []
    for k in range(2, max(3, min(MAX_K, len(z) // 10) + 1)):
        labels = KMeans(k, n_init=10, random_state=0).fit_predict(z)
        if len(set(labels[sample])) < 2:
            continue
        tried.append((float(silhouette_score(z[sample], labels[sample])), k, labels))
    if not tried:
        raise ToolError("Could not form groups.", invalid=False)
    best = max(tried, key=lambda t: t[0])
    score, k, labels = min((t for t in tried if t[0] >= 0.9 * best[0]), key=lambda t: t[1])  # the simplest that is about as clear
    order = pd.Series(labels).value_counts().index  # group 1 = the largest
    group_of = pd.Series(labels).map({g: i + 1 for i, g in enumerate(order)}).to_numpy()
    clarity = "clearly separated" if score >= 0.5 else "moderately separated" if score >= 0.25 else "overlapping"
    zdf = pd.DataFrame(z, columns=x.columns, index=table.index)
    overall = table.median()
    groups, rows = [], []
    for g in range(1, k + 1):
        mask = group_of == g
        diff = zdf[mask].mean().sort_values(key=abs, ascending=False)
        traits = []
        for c in diff.index[:3]:
            if abs(diff[c]) < 0.3:
                continue
            med, base = float(table[c][mask].median()), float(overall[c])
            if med == base:  # skewed: the medians tie, the averages tell them apart
                trait = f"{number(float(table[c][mask].mean()))} on average (overall {number(float(table[c].mean()))})"
            else:
                trait = f"{number(med)} (typical {number(base)})"
            traits.append(f"{'high' if diff[c] > 0 else 'low'} {c}: {trait}")
        medians = {c: number(float(table[c][mask].median())) for c in table.columns}
        groups.append({"group": g, "size": int(mask.sum()), "share": number(float(mask.mean()), 3),
                       "stands_out": "; ".join(traits) or "close to typical on every number", "medians": medians})
        rows.append([g, int(mask.sum()), number(float(mask.mean()), 3), *medians.values()])
    out = table.copy()
    out.insert(0, "segment", group_of)
    out = out.reset_index()
    saved = ws.add_table(out, name=ws.derived_name(data, "segments"), parent=data, meta={"segments": k})
    result = {"what": noun, "count": len(table), "described_by": list(table.columns), "how": how, "groups": groups,
              "separation": {"silhouette": number(score, 2), "meaning": f"{clarity} (silhouette: 1 = distinct, "
                             "0 = overlapping)"},
              "saved_as": saved, "saved_note": f"{saved} has each one's segment number and numbers."}
    if score < 0.25:
        result["note"] = "The groups overlap: treat them as rough bands along a scale, not distinct types."
    artifact = {"type": "table", "title": f"{noun.capitalize()} in {k} segments",
                "columns": ["segment", "size", "share", *table.columns], "rows": rows,
                "notes": [f"{len(table):,} {noun}, {clarity} (silhouette {score:.2f})."]}
    return result, artifact


def _profile_table(df, roles, entity, columns):
    """One row per thing to group, numbers only: (table, noun, how it was built)."""
    if entity and df[entity].nunique() < len(df.dropna(subset=[entity])):
        frame = df.dropna(subset=[entity])
        keys = frame[entity].astype(str)
        g = frame.groupby(keys)
        cols = {}
        order = roles.get("order")
        cols["orders" if order and order != entity else "rows"] = (
            g[order].nunique() if order and order != entity else g.size()).astype(float)
        amount = _amount(frame, roles)
        if amount is not None:
            cols["amount"] = amount.groupby(keys).sum()
            if "orders" in cols:
                cols["amount_per_order"] = cols["amount"] / cols["orders"]
        date = roles.get("date")
        if date:
            last, first = g[date].max(), g[date].min()
            cols["days_since_last"] = (frame[date].max() - last).dt.days.astype(float)
            cols["days_active"] = ((last - first).dt.days + 1).astype(float)
        for c in columns or []:
            if c != entity and _numeric(frame[c]):
                cols[f"mean_{c}"] = g[c].mean().astype(float)
        table = pd.DataFrame(cols)
        table.index.name = entity
        how = (f"each {entity} described by its rows: " + ", ".join(table.columns)
               + (" (amount = quantity x price)" if amount is not None and not roles.get("amount") else ""))
        return table, f"{entity} values", how
    numbers = columns or [c for c in df.columns if _numeric(df[c]) and df[c].nunique() > 2
                          and not (df[c].nunique() == len(df) and (df[c].dropna() % 1 == 0).all())][:FEATURES]
    numbers = [c for c in numbers if _numeric(df[c])]
    if not numbers:
        raise ToolError("There are no number columns to group by. Give group=a column to describe (e.g. a customer "
                        "id) or columns=[numbers].", field="columns", invalid=False)
    table = df[numbers].astype(float)
    if entity:
        table.index = df[entity].astype(str)
        table.index.name = entity
    return table, "rows", "each row described by " + ", ".join(numbers)


def _amount(frame: pd.DataFrame, roles: dict) -> pd.Series | None:
    if roles.get("amount"):
        return frame[roles["amount"]].astype(float)
    if roles.get("quantity") and roles.get("price"):
        return frame[roles["quantity"]].astype(float) * frame[roles["price"]].astype(float)
    if roles.get("quantity"):
        return frame[roles["quantity"]].astype(float)
    return None


# ---------------------------------------------------------------- unusual

def _unusual(ws, data, df, roles, value, by, columns, time_unit):
    date = roles.get("date")
    if by and pd.api.types.is_datetime64_any_dtype(df[by]):  # 'by date' means: per date
        date, by = by, None
    if not date and not columns and (split := split_time(df)):  # year + month columns: combined here
        df, date = df.assign(_period=split_dates(df, *split)), "_period"
    if date and columns and len(columns) == 1 and not value and _numeric(df[columns[0]]):
        value, columns = columns[0], None  # one number in a table with dates: watched per period
    if not date or columns:
        return _unusual_rows(ws, data, df, columns)
    if value and not _numeric(df[value]):
        raise ToolError(f"'{value}' is not a number column.", field="value")
    frame = df.dropna(subset=[date] + ([value] if value else []))
    freq = CODE[time_unit] if time_unit != "auto" else _grain(frame[date])
    agg = "sum" if value else "count"
    label, word = value or "rows", WORD[freq]
    if by:
        keys = frame[by].astype(str)
        size = (frame.groupby(keys)[value].sum() if value else keys.value_counts()).abs()
        names = [str(g) for g in size.sort_values(ascending=False).index[:MAX_GROUPS]]
        parts = [(g, frame[keys == g]) for g in names]
    else:
        parts = [(None, frame)]
    found, chart, notes, periods = [], None, [], 0
    for name, sub in parts:
        y, series_notes = _series(sub, date, value, agg, freq)
        notes += series_notes if name is None else []
        if len(y) < 8:
            continue
        periods += len(y)
        events, expected, flagged = _unusual_periods(y, freq)
        found += [{**({"group": name} if name is not None else {}), **e} for e in events]
        if chart is None:
            chart = {"type": "chart", "chart": "unusual", "title": f"{label} per {word}" + (f" ({name})" if name else ""),
                     "x": [d.strftime("%Y-%m-%d") for d in y.index], "y": [number(v) for v in y],
                     "expected": [number(v) for v in expected], "flagged": [d.strftime("%Y-%m-%d") for d in flagged],
                     "x_label": word, "y_label": f"sum of {label}" if value else "rows"}
    if chart is None:
        raise ToolError(f"Fewer than 8 {word}s of data; too short to tell what is unusual.", invalid=False)
    found.sort(key=lambda e: -abs(e["score"]))
    result = {"looked_at": f"{label} per {word}" + (f", for the {len(parts)} largest {by} groups" if by else ""),
              "periods": periods, "unusual_events": len(found), "events": found[:10],
              "method": "each period against its trend and season (and against the whole history); score = how many "
                        f"times the normal deviation; unusual from {THRESHOLD:g}"}
    if not found:
        result["finding"] = f"No unusual {word}s: every value is within the normal range of its trend and season."
    if notes:
        result["notes"] = notes
    return result, chart


def _grain(dates: pd.Series) -> str:
    """Unusual days are looked for in days: the data's own grain, coarsened only when it gives too many."""
    freq = native_freq(dates)
    days = (dates.max() - dates.min()).days + 1
    per = {"D": 1, "W": 7, "M": 30.4, "Q": 91.3, "Y": 365.25}
    while days / per[freq] > MAX_PERIODS and freq != "Y":
        freq = ORDER[ORDER.index(freq) + 1]
    return freq


def _unusual_periods(y: pd.Series, freq: str):
    logged = bool((y >= 0).all() and y.skew() > 2)  # spiky counts: judged on a log scale
    work = np.log1p(y) if logged else y
    expected = _expected(work, freq)
    local = _robust_z(work - expected)  # unusual for its moment (trend and season)
    overall = _robust_z(work - work.median())  # extreme against the whole history
    if logged:
        expected = np.expm1(expected)
    # In long series at most ~0.5% of periods can be unusual for their moment: the bar rises to the
    # 99.5th percentile when needed. Values extreme against all history always count.
    cutoff = max(THRESHOLD, float(local.abs().quantile(0.995))) if len(local) >= 200 else THRESHOLD
    flagged = (local.abs() > cutoff) | (overall.abs() > THRESHOLD)
    z = local.where(local.abs() >= overall.abs(), overall)
    events = []
    for start, end in _runs(flagged):
        peak = z[start:end].abs().idxmax()
        actual, exp = float(y[peak]), float(expected[peak])
        span = {"from": start.strftime("%Y-%m-%d"), "until": end.strftime("%Y-%m-%d")} if end != start else {}
        events.append({"period": peak.strftime("%Y-%m-%d"), **span,
                       "value": number(actual), "expected": number(exp),
                       "change_pct": number(100 * (actual - exp) / abs(exp), 1) if exp else None,
                       "direction": "above" if actual > exp else "below", "score": number(float(abs(z[peak])), 1)})
    return events, expected, y.index[flagged.to_numpy()]


def _expected(y: pd.Series, freq: str) -> pd.Series:
    """A robust trend (rolling median) plus the typical seasonal pattern (median per position)."""
    season = SEASON[freq] if len(y) >= 2 * SEASON[freq] + 1 and SEASON[freq] > 1 else 1
    window = max(3, min(season if season > 1 else 7, len(y) // 2 * 2 - 1)) | 1
    trend = y.rolling(window, center=True, min_periods=1).median()
    if season == 1:
        return trend
    pos = pd.Series(_positions(y.index, freq), index=y.index)
    seasonal = (y - trend).groupby(pos).transform("median")
    trend = (y - seasonal).rolling(window, center=True, min_periods=1).median()
    return trend + (y - trend).groupby(pos).transform("median")


def _runs(flags: pd.Series) -> list[tuple]:
    """Consecutive flagged periods as one event: [(first, last)]."""
    out, start, prev = [], None, None
    for when, on in flags.items():
        if on and start is None:
            start = when
        if not on and start is not None:
            out.append((start, prev))
            start = None
        prev = when
    if start is not None:
        out.append((start, prev))
    return out


def _unusual_rows(ws, data, df, columns):
    cols = columns or [c for c in df.columns if _numeric(df[c]) and df[c].nunique() > 2
                       and not (df[c].nunique() == len(df) and (df[c].dropna() % 1 == 0).all())]
    cols = [c for c in cols if _numeric(df[c])]
    if not cols:
        raise ToolError("There are no number columns to check.", field="columns", invalid=False)
    if len(df) < MIN_ITEMS:
        raise ToolError(f"Too few rows to tell what is unusual (at least {MIN_ITEMS} are needed).", invalid=False)
    z = pd.DataFrame({c: _robust_z(_log_if_skewed(df[c].astype(float))) for c in cols}).abs()
    score = z.max(axis=1)
    flagged = score[score > THRESHOLD].sort_values(ascending=False)
    worst = z.loc[flagged.index].idxmax(axis=1)
    rows = []
    for i in flagged.index[:10]:
        c = worst[i]
        rows.append({"row": int(i) + 1, "column": c, "value": number(float(df.at[i, c])),
                     "typical": number(float(df[c].median())), "score": number(float(flagged[i]), 1)})
    result = {"looked_at": f"{len(cols)} number columns across {len(df):,} rows: " + ", ".join(cols[:8]),
              "unusual_rows": len(flagged), "share": number(len(flagged) / len(df), 4), "top": rows,
              "method": f"robust z-score per column (log scale for skewed numbers); unusual from {THRESHOLD:g}"}
    if len(flagged):
        out = df.loc[flagged.index].copy()
        out.insert(0, "unusual_in", worst.to_numpy())
        out.insert(1, "score", flagged.round(1).to_numpy())
        result["saved_as"] = ws.add_table(out.reset_index(names="row"), name=ws.derived_name(data, "unusual rows"),
                                          parent=data)
    else:
        result["finding"] = "No row is extreme on any of these columns."
    artifact = {"type": "table", "title": f"Unusual rows of {ws.describe(data)}",
                "columns": ["row", "column", "value", "typical", "score"],
                "rows": [[r["row"], r["column"], r["value"], r["typical"], r["score"]] for r in rows]}
    return result, artifact


# ---------------------------------------------------------------- together

def _together(ws, data, df, roles, basket, item):
    basket = basket or roles.get("order")
    item = item or roles.get("item")
    if not basket or not item:
        missing = " and ".join(w for w, c in (("basket (the order, visit or case column)", basket),
                                               ("item (what is in it)", item)) if not c)
        raise ToolError(f"Give {missing}: this table's roles don't name one. Columns: {', '.join(map(str, df.columns))}.",
                        field="basket" if not basket else "item")
    lines = df[[basket, item]].dropna().astype(str).drop_duplicates()
    n = lines[basket].nunique()
    size = lines.groupby(basket)[item].transform("size")
    counts = lines.groupby(item)[basket].nunique()
    multi = lines[(size >= 2) & (size <= MAX_BASKET)]
    n_multi = multi[basket].nunique()
    if n_multi < MIN_ITEMS:
        raise ToolError(f"Only {n_multi} {basket} values have two or more different {item} values; at least "
                        f"{MIN_ITEMS} are needed.", invalid=False)
    top = counts.sort_values(ascending=False).index[:TOP_ITEMS]
    notes = []
    if len(counts) > TOP_ITEMS:
        notes.append(f"Pairs among the {TOP_ITEMS} most common of {len(counts):,} {item} values.")
    multi = multi[multi[item].isin(top)]
    codes = pd.Categorical(multi[item], categories=sorted(top))
    m = pd.DataFrame({"b": multi[basket].to_numpy(), "i": codes.codes})
    pairs = m.merge(m, on="b")
    pairs = pairs[pairs["i_x"] < pairs["i_y"]]
    together = pairs.groupby(["i_x", "i_y"]).size()
    min_count = max(3, int(0.002 * n))
    together = together[together >= min_count]
    if together.empty:
        result = {"baskets": n, "with_2_or_more": n_multi, "pairs_tested": 0,
                  "finding": f"No pair of {item} values occurs together in at least {min_count} {basket} values."}
        return result, {"type": "table", "title": f"{item} bought together", "columns": [], "rows": []}
    names = np.array(codes.categories)
    a, b = names[together.index.get_level_values(0)], names[together.index.get_level_values(1)]
    ca, cb, c = counts[a].to_numpy(), counts[b].to_numpy(), together.to_numpy()
    lift = c * n / (ca * cb)
    p = stats.hypergeom.sf(c - 1, n, ca, cb)  # one-sided Fisher exact: together at least this often
    q = _bh(p)
    table = pd.DataFrame({"a": a, "b": b, "together": c, "lift": lift.round(2), "share_of_a_with_b": (c / ca).round(3),
                          "share_of_b_with_a": (c / cb).round(3), "q_value": q})
    strong = table[(table.lift >= 1.2) & (table.q_value < 0.05)].sort_values(["lift", "together"], ascending=False)
    result = {"basket": basket, "item": item, "baskets": n, "with_2_or_more": n_multi, "pairs_tested": len(table),
              "pairs_found": len(strong),
              "top_pairs": [{"a": r.a, "b": r.b, "together": int(r.together), "lift": number(r.lift, 2),
                             "share_of_a_with_b": number(r.share_of_a_with_b, 3), "q_value": number(r.q_value, 4)}
                            for r in strong.head(10).itertuples()],
              "method": "lift = how many times more often than chance; q = false-discovery-controlled p across "
                        f"all {len(table):,} pairs tested; a pair counts from lift 1.2 and q 0.05"}
    if strong.empty:
        result["finding"] = f"No pair of {item} values occurs together more than chance would explain."
    else:
        result["saved_as"] = ws.add_table(strong.head(1000).reset_index(drop=True),
                                          name=ws.derived_name(data, "pairs"), parent=data)
    if notes:
        result["notes"] = notes
    artifact = {"type": "table", "title": f"{item} that go together (per {basket})",
                "columns": ["a", "b", "together", "lift", "share of a with b"],
                "rows": [[r.a, r.b, int(r.together), number(r.lift, 2), number(r.share_of_a_with_b, 3)]
                         for r in strong.head(25).itertuples()]}
    return result, artifact


def _bh(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values."""
    p = np.asarray(p, float)
    order = np.argsort(p)
    ranked = p[order] * len(p) / np.arange(1, len(p) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty_like(q)
    out[order] = np.minimum(q, 1.0)
    return out
