"""predict: learn a column from the others, say how well that works and what drives it, and save the
predictions as a new table.

The model decides what to predict; this code decides how, the same way every time:
- task: yes/no or a few classes -> classification; a number -> regression
- inputs: the other columns, minus ids, free text, constants and dates (reported with reasons);
  month and weekday come from the date column when there is one
- models: a regularised linear model (logistic / ridge regression) and gradient boosting
  (scikit-learn HistGradientBoosting) are tested the same way and the better one is kept - on a few
  hundred rows boosting overfits and the linear model wins; with more data and non-linear
  patterns boosting does. The reported score is the winner's, so it is slightly optimistic.
- test: with a date column, learn from the earlier 80% of the time span and score on the latest
  20% (no peeking at the future); otherwise 5-fold cross-validation. Over MAX_TRAIN rows, a random
  MAX_TRAIN of them are used (said in the result)
- always against a baseline (the average / the most common class): the verdict says plainly when
  the model is not better
- a column that predicts the target almost perfectly on its own (a number by rank; a category or a
  yes/no column by its groups) is reported as a likely leak, and so is a near-perfect score that one
  column carries almost alone
- drivers: permutation importance (how much the score drops when a column is shuffled) with the
  direction the model gives it with the other columns held as they are (partial dependence: every
  row set to a low then a high value, or to each category) - so a column is not credited with what
  another one does (big diamonds tend to have poor clarity). Associations, not causes
- saved: the table with a predicted column (and the probability, for yes/no). Rows whose outcome is
  known and that the test scored get the prediction of a model that did not see them; the others
  (rows without the outcome, rows the model learned from) get the final model's. new_data, another
  table with the same columns, is scored the same way.

Ported from the analyst project (analyst/ops/predict.py, column mode, and learn.py's drivers); the
linear candidate and the saved predictions are new.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy import stats

from .chart_image import save_chart
from .core import ToolError, ToolResult, number, ok, tool
from .workspace import Workspace, resolve_column

MIN_ROWS = 50
MAX_CLASSES = 10
MAX_TRAIN = 50_000
TOP_CATEGORIES = 30
LEAK_SCORE = 0.98
NEAR_PERFECT = {"AUC": 0.98, "accuracy": 0.98, "R2": 0.95}
DOMINANT = 0.8  # a driver's share of the importance that makes a near-perfect score suspect
MARGIN = 0.02  # how much better than the baseline counts as better


# ---------------------------------------------------------------- features

def _numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _made_from(ws: Workspace, data: str, target: str) -> set[str]:
    """The columns a target was computed from, when the table was saved by a query that defines it
    (e.g. CASE WHEN affairs > 0 THEN 'yes' ... AS had_affair): learning from them is a leak."""
    import sqlglot
    from sqlglot import exp

    sql = ws.entry(data).get("meta", {}).get("sql")
    if not sql:
        return set()
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return set()
    sources = set()
    for alias in tree.find_all(exp.Alias):
        if alias.alias.lower() == target.lower():
            sources |= {c.name for c in alias.this.find_all(exp.Column)}
    return {c for c in sources if c.lower() != target.lower()}


def _choose_features(df: pd.DataFrame, target: str, time_col: str | None, wanted: list[str] | None,
                     made_from: set[str] = frozenset()):
    """Columns to learn from, and the ones left out with the reason."""
    excluded = []
    if time_col and not wanted:
        excluded.append({"column": time_col, "reason": "the date: it sets the test split, and month and weekday "
                                                      "come from it"})
    cols = wanted or [c for c in df.columns if c not in (target, time_col)]
    use = []
    for c in cols:
        s = df[c]
        present = s.dropna()
        if c == target:
            continue
        if c == f"{target}_outlier":  # made from the target by clean_data's flag_outliers
            excluded.append({"column": c, "reason": "made from the target (a leak)"})
            continue
        if c in made_from:  # also when asked for: it would predict the target from itself
            excluded.append({"column": c, "reason": f"{target} was computed from it (a leak)"})
            continue
        if present.empty:
            reason = "no values"
        elif present.nunique() == 1:
            reason = "a single value"
        elif pd.api.types.is_datetime64_any_dtype(s):
            reason = "a date (month and weekday are used instead)" if c == time_col else "a date"
        elif present.nunique() == len(present) and len(present) > 20 and (
                not _numeric(s) or (present.astype(float) % 1 == 0).all()):
            reason = "unique per row (an id or free text)"
        elif not _numeric(s) and not pd.api.types.is_bool_dtype(s) and present.nunique() > 0.5 * len(present):
            reason = "mostly unique text"
        else:
            use.append(c)
            continue
        excluded.append({"column": c, "reason": reason})
    return use, excluded


def _spec(df: pd.DataFrame, cols: list[str], time_col: str | None) -> dict:
    spec = {"columns": {}, "time_column": None, "time_features": []}
    for c in cols:
        s = df[c]
        if _numeric(s) or pd.api.types.is_bool_dtype(s):
            spec["columns"][c] = {"kind": "number"}
        else:
            top = s.astype(str).where(s.notna(), "missing").value_counts().index[:TOP_CATEGORIES].tolist()
            spec["columns"][c] = {"kind": "category", "categories": sorted(set(top) | {"other", "missing"})}
    if time_col:
        t = df[time_col].dropna()
        spec["time_column"] = time_col
        spec["time_features"] = [f for f, ok_ in (("month", t.dt.month.nunique() > 1),
                                                  ("weekday", t.dt.dayofweek.nunique() > 1)) if ok_]
    return spec


def _build(df: pd.DataFrame, spec: dict) -> tuple[pd.DataFrame, list[str]]:
    """The model's input frame from any table; returns (X, columns the table lacked)."""
    X = pd.DataFrame(index=df.index)
    missing = []
    for c, info in spec["columns"].items():
        if c not in df:
            missing.append(c)
        if info["kind"] == "number":
            # to_numpy handles the nullable Int64 / boolean columns (NA -> NaN)
            X[c] = df[c].to_numpy(dtype=float, na_value=np.nan) if c in df else np.nan
        else:
            s = df[c].astype(str).where(df[c].notna(), "missing") if c in df else pd.Series("missing", index=df.index)
            s = s.where(s.isin(info["categories"]), "other")
            X[c] = pd.Categorical(s, categories=info["categories"])
    t = spec["time_column"]
    for f in spec["time_features"]:
        if t not in df:
            missing.append(t)
            X[f] = np.nan
        else:
            X[f] = (df[t].dt.month if f == "month" else df[t].dt.dayofweek).astype(float)
    return X, sorted(set(missing))


# ---------------------------------------------------------------- predict

@tool
def predict(ws: Workspace, data: str, target: str, features: list[str] | None = None,
            time_column: str | None = None, new_data: str | None = None) -> ToolResult:
    """Learn ``target`` from the other columns, test it, find its drivers; save the predictions."""
    from sklearn.metrics import accuracy_score, mean_absolute_error, r2_score, roc_auc_score

    df = ws.table(data)
    target = resolve_column(df, target, "target")
    wanted = [resolve_column(df, c, "features") for c in features] if features else None
    time_col = _time_column(df, time_column)
    frame = df.dropna(subset=[target])
    if len(frame) < MIN_ROWS:
        raise ToolError(f"Only {len(frame)} rows have {target}; learning it needs at least {MIN_ROWS}.", invalid=False)
    notes = []
    if len(frame) > MAX_TRAIN:
        notes.append(f"Learned and tested on a random {MAX_TRAIN:,} of the {len(frame):,} rows with {target}.")
        frame = frame.sample(MAX_TRAIN, random_state=0).sort_index()

    y_raw = frame[target]
    classify = not _numeric(y_raw) or y_raw.nunique() == 2
    if classify:
        y = y_raw.astype(str)
        classes = sorted(y.unique())
        if len(classes) > MAX_CLASSES:
            raise ToolError(f"{target} has {len(classes)} different values; a category to predict can have at most "
                            f"{MAX_CLASSES}. For a number, the column must hold numbers.", field="target")
        if len(classes) < 2:
            raise ToolError(f"{target} has a single value; there is nothing to predict.", invalid=False)
        counts = y.value_counts()
        positive = str(counts.index[-1]) if len(classes) == 2 else None  # the rarer class: churned, fraud ...
        if counts.min() < 5:
            raise ToolError(f"Only {counts.min()} rows have {target} = {counts.idxmin()}; each value needs at least 5.",
                            invalid=False)
    else:
        y = y_raw.astype(float)
        classes, positive = None, None
    binary = classify and len(classes) == 2

    cols, excluded = _choose_features(frame, target, time_col, wanted, _made_from(ws, data, target))
    spec = _spec(frame, cols, time_col)
    X, _ = _build(frame, spec)
    if X.shape[1] == 0:
        raise ToolError("No usable columns are left to predict it from.", invalid=False)

    def predict_scores(m, Xt):
        if binary:
            return m.predict_proba(Xt)[:, list(m.classes_).index(positive)]
        return m.predict(Xt)

    def evaluate(y_true, pred) -> float:
        if binary:
            return float(roc_auc_score(y_true == positive, pred))
        return float(accuracy_score(y_true, pred) if classify else r2_score(y_true, pred))

    splits, tested_on = _splits(frame, time_col)
    candidates = _candidates(X, classify)
    preds = {name: [] for name in candidates}
    bases, truth = [], []
    for train, test in splits:
        for name, make in candidates.items():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                m = make().fit(X.loc[train], y.loc[train])
            preds[name].append(pd.Series(predict_scores(m, X.loc[test]), index=test))
        if binary:
            bases.append(np.full(len(test), (y.loc[train] == positive).mean()))
        elif classify:
            bases.append(np.full(len(test), y.loc[train].mode().iloc[0], dtype=object))
        else:
            bases.append(np.full(len(test), y.loc[train].mean()))
        truth.append(y.loc[test])
    y_test, b_all = pd.concat(truth), np.concatenate(bases)
    if binary and y_test.nunique() < 2:
        raise ToolError("The test period has only one outcome; there are too few of the rarer class to test.",
                        invalid=False)
    scores = {name: evaluate(y_test, pd.concat(p).to_numpy()) for name, p in preds.items()}
    chosen = max(scores, key=scores.get)
    tested = pd.concat(preds[chosen])  # out-of-sample predictions, by row
    score = scores[chosen]

    extra = {}
    if binary:
        metric, base = "AUC", 0.5
    elif classify:
        metric, base = "accuracy", float(accuracy_score(y_test, b_all))
    else:
        metric, base = "R2", float(r2_score(y_test, b_all))
        extra = {"average_error": number(float(mean_absolute_error(y_test, tested.loc[y_test.index]))),
                 "baseline_average_error": number(float(mean_absolute_error(y_test, b_all)))}
    better = score > base + MARGIN
    verdict = _verdict(metric, score, better)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        full = candidates[chosen]().fit(X, y)
    drivers = _drivers(full, X, y, binary, positive, classify, target) if better else []
    leaks = _leaks(X, y, classify, binary, positive)

    bundle = {"model": full, "spec": spec, "target": target, "classify": classify, "binary": binary,
              "positive": positive}
    summary = _score_table(ws, data, df, bundle, tested, f"{len(tested):,} rows scored by the test")
    result = {"data": data, "target": target, "task": "classification" if classify else "regression",
              "rows": len(frame), "tested_on": tested_on, "model_type": chosen, "metric": metric,
              "score": number(score), "baseline": number(base), **extra,
              "scores_by_model": {k: number(v) for k, v in scores.items()}, "better_than_baseline": better,
              "verdict": verdict, "drivers": drivers, "excluded_columns": excluded, "predictions": summary}
    if binary:
        result["positive_class"] = positive
    if new_data:
        result["new_data"] = _score_new(ws, new_data, bundle, better)
    warning = ws.issues_warning(data)
    if warning:
        result["data_warning"] = warning
    if leaks:
        result["warning"] = (f"{', '.join(leaks)} predict {target} almost perfectly on their own: check they are not "
                             "recorded after the outcome or copied from it (a leak). If so, run it again without them "
                             "(features=[...]).")
    elif better and score >= NEAR_PERFECT[metric] and drivers and drivers[0]["share"] >= DOMINANT:
        result["warning"] = (f"{drivers[0]['column']} alone carries a near-perfect prediction: check it is known before "
                             f"{target} and not computed from it or recorded after it (a leak).")
    if drivers:
        notes.append("Drivers are associated with the outcome; they are not proven causes.")
    if not better:
        notes.append("The predictions are no more useful than the average guess: say so rather than use them.")
    if notes:
        result["notes"] = notes
    if not drivers:  # nothing worth drawing: the verdict says why
        return ok(result, {"type": "table", "title": f"What predicts {target}", "columns": ["column", "importance"],
                           "rows": [], "notes": [verdict]})
    # The drivers as a chart file, like forecast's and find_patterns' (measured: without it the analyst copied the
    # importances into a script and a table of its own to chart them - two working files, numbers typed by hand).
    artifact = {"type": "chart", "chart": "bar", "title": f"What predicts {target}", "x_label": "column",
                "y_label": "share of the prediction", "x": [d["column"] for d in drivers],
                "y": [d["share"] for d in drivers], "notes": [verdict]}
    artifact["id"] = result["chart_file"] = save_chart(ws, artifact, parent=data)
    return ok(result, artifact)


def _score_table(ws: Workspace, data: str, df: pd.DataFrame, bundle: dict, tested: pd.Series, how: str) -> dict:
    """The table with the predictions added; rows the test scored keep their out-of-sample prediction."""
    X, _ = _build(df, bundle["spec"])
    m, target = bundle["model"], bundle["target"]
    out = df.copy()
    col = f"predicted_{target}"
    if bundle["binary"]:
        proba = pd.Series(m.predict_proba(X)[:, list(m.classes_).index(bundle["positive"])], index=df.index)
        proba.loc[tested.index] = tested
        out[f"probability_{bundle['positive']}"] = proba.round(4)
        others = [c for c in m.classes_ if c != bundle["positive"]]
        out[col] = np.where(proba >= 0.5, bundle["positive"], others[0])
    else:
        values = pd.Series(m.predict(X), index=df.index, dtype=object if bundle["classify"] else float)
        values.loc[tested.index] = tested
        out[col] = values
    new_id = ws.add_table(out, name=ws.derived_name(data, f"predicted {target}"), parent=data,
                          meta={"predicted": target})
    unknown = df[target].isna()
    summary = {"saved_as": new_id, "column": col, "how": f"{how}: their prediction comes from a model that did not "
               f"see them; the other rows from the final model"}
    if bundle["binary"]:
        summary["probability_column"] = f"probability_{bundle['positive']}"
    if unknown.any():
        summary["rows_without_target"] = int(unknown.sum())
        summary.update(_prediction_summary(out.loc[unknown], col, bundle))
    return summary


def _prediction_summary(rows: pd.DataFrame, col: str, bundle: dict) -> dict:
    if bundle["binary"]:
        p = rows[f"probability_{bundle['positive']}"]
        return {"average_probability": number(float(p.mean())), "predicted_counts": rows[col].value_counts().to_dict()}
    if bundle["classify"]:
        return {"predicted_counts": rows[col].value_counts().to_dict()}
    v = rows[col].astype(float)
    return {"average_prediction": number(float(v.mean())), "min": number(float(v.min())), "max": number(float(v.max()))}


def _score_new(ws: Workspace, new_data: str, bundle: dict, better: bool) -> dict:
    df = ws.table(new_data)
    X, missing = _build(df, bundle["spec"])
    if len(missing) == len(bundle["spec"]["columns"]) + bool(bundle["spec"]["time_features"]):
        raise ToolError(f"{new_data} has none of the columns the model learned from "
                        f"({', '.join(bundle['spec']['columns'])}).", field="new_data", invalid=False)
    m, target = bundle["model"], bundle["target"]
    out = df.copy()
    col = f"predicted_{target}"
    if bundle["binary"]:
        out[f"probability_{bundle['positive']}"] = m.predict_proba(X)[:, list(m.classes_).index(bundle["positive"])].round(4)
    out[col] = m.predict(X)
    new_id = ws.add_table(out, name=ws.derived_name(new_data, f"predicted {target}"), parent=new_data,
                          meta={"predicted": target})
    res = {"saved_as": new_id, "rows": len(out), "column": col, **_prediction_summary(out, col, bundle)}
    if missing:
        res["warning"] = f"{new_data} lacks {', '.join(missing)}: predicted as if unknown, so less accurate."
    if not better:
        res["warning"] = (res.get("warning", "") + " The model was not better than the baseline: these predictions "
                          "are no more useful than the average.").strip()
    return res


def _candidates(X: pd.DataFrame, classify: bool) -> dict:
    """Model factories: a regularised linear model and gradient boosting."""
    from sklearn.compose import make_column_transformer
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    cats = [c for c in X.columns if str(X[c].dtype) == "category"]
    nums = [c for c in X.columns if c not in cats]

    def linear():
        prep = make_column_transformer(
            (OneHotEncoder(handle_unknown="ignore"), cats),
            (make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler()), nums))
        return make_pipeline(prep, LogisticRegression(C=1.0, max_iter=2000) if classify else Ridge(alpha=1.0))

    def boosting():
        kw = dict(max_iter=300, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=20,
                  l2_regularization=1.0, categorical_features="from_dtype", random_state=0)
        return HistGradientBoostingClassifier(**kw) if classify else HistGradientBoostingRegressor(**kw)

    return {"linear": linear, "gradient boosting": boosting}


def _time_column(df: pd.DataFrame, wanted: str | None) -> str | None:
    if wanted:
        c = resolve_column(df, wanted, "time_column")
        if not pd.api.types.is_datetime64_any_dtype(df[c]):
            raise ToolError(f"'{c}' is not a date column.", field="time_column")
        return c
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    return dates[0] if len(dates) == 1 else None


def _splits(frame: pd.DataFrame, time_col: str | None):
    """Earlier 80% of the time span -> latest 20% when there are dates; else 5-fold CV."""
    if time_col and frame[time_col].notna().all() and frame[time_col].nunique() > 10:
        t = frame[time_col]
        cut = t.min() + 0.8 * (t.max() - t.min())
        train, test = frame.index[t <= cut], frame.index[t > cut]
        if len(train) >= 30 and len(test) >= 10:
            return [(train, test)], f"the latest 20% of the time span ({cut:%Y-%m-%d} on), learned from the earlier 80%"
    from sklearn.model_selection import KFold
    folds = [(frame.index[a], frame.index[b]) for a, b in KFold(5, shuffle=True, random_state=0).split(frame)]
    return folds, "5-fold cross-validation (each row predicted by a model that did not see it)"


def _verdict(metric: str, score: float, better: bool) -> str:
    if not better:
        return "Not clearly better than the baseline: treat its predictions as no better than the average guess."
    if metric == "AUC":
        quality = "a strong ranking" if score >= 0.8 else "a useful but rough ranking" if score >= 0.7 else "a weak ranking"
        return f"Better than the baseline: {quality} (AUC {score:.2f}; 0.5 = guessing)."
    if metric == "accuracy":
        return f"Better than always guessing the most common class ({score:.0%} correct)."
    quality = "strong" if score >= 0.6 else "useful but rough" if score >= 0.3 else "weak - most of it can't be predicted from these columns"
    return f"Better than the baseline, explaining {score:.0%} of the variation: {quality}."


def _leaks(X, y, classify, binary, positive) -> list[str]:
    from sklearn.metrics import roc_auc_score
    out = []
    for c in X.columns:
        x = X[c]
        if str(x.dtype) == "category" or x.nunique() < 3:  # its groups alone sort the outcome (alive -> survived)
            if classify and _purity(x, y) >= LEAK_SCORE and x.nunique() < len(x) / 5:
                out.append(c)
            continue
        if classify and not binary:
            continue
        ok_ = x.notna() & y.notna()
        if ok_.sum() < 20 or x[ok_].nunique() < 3:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            s = (abs(roc_auc_score(y[ok_] == positive, x[ok_]) - 0.5) * 2 if binary
                 else abs(stats.spearmanr(x[ok_], y[ok_])[0]))
        if s >= LEAK_SCORE:
            out.append(c)
    return out


def _purity(x: pd.Series, y: pd.Series) -> float:
    """Share of rows whose outcome is their group's most common one."""
    table = pd.crosstab(x, y)
    return float(table.max(axis=1).sum() / table.to_numpy().sum()) if table.size else 0.0


def _drivers(model, X, y, binary, positive, classify, target, top: int = 5) -> list[dict]:
    """The columns the model leans on most (permutation importance), each with its direction."""
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import roc_auc_score

    def auc(m, a, b):
        return roc_auc_score(b == positive, m.predict_proba(a)[:, list(m.classes_).index(positive)])

    sample = X.sample(min(len(X), 3000), random_state=0)
    res = permutation_importance(model, sample, y.loc[sample.index], n_repeats=5, random_state=0,
                                 scoring=auc if binary else None)
    imp = pd.Series(np.clip(res.importances_mean, 0, None), index=X.columns)
    if imp.sum() <= 0:
        return []
    imp = imp / imp.sum()
    if binary:
        def score(rows):
            return float(model.predict_proba(rows)[:, list(model.classes_).index(positive)].mean())
        what = f"chance of {target} = {positive}"
    elif classify:
        score, what = None, None  # several classes: no single up or down
    else:
        def score(rows):
            return float(np.mean(model.predict(rows)))
        what = f"predicted {target}"
    rows = sample.head(1000)
    out = []
    for c, share in imp.sort_values(ascending=False).head(top).items():
        if share < 0.03:
            continue
        out.append({"column": c, "share": number(float(share), 3),
                    "direction": _direction(rows, c, score, what) if score else "differs by class"})
    return out


def _direction(rows: pd.DataFrame, c: str, score, what: str) -> str:
    """Partial dependence: the average prediction with every row's c set to one value, other columns kept."""
    x = rows[c]
    if str(x.dtype) == "category":
        counts = x.value_counts()
        values = [v for v in counts.index if counts[v] >= 10][:12] or list(counts.index[:12])
        if len(values) < 2:
            return "no clear pattern"
        means = {v: score(rows.assign(**{c: pd.Categorical([v] * len(rows), categories=x.cat.categories)}))
                 for v in values}
        return f"highest {what} for {max(means, key=means.get)}, lowest for {min(means, key=means.get)}"
    grid = np.unique(np.nanquantile(x.astype(float), [0.1, 0.3, 0.5, 0.7, 0.9]))
    if len(grid) < 2:
        return "no clear pattern"
    means = [score(rows.assign(**{c: v})) for v in grid]
    rho = stats.spearmanr(grid, means)[0] if np.ptp(means) > 0 else 0.0
    if not np.isfinite(rho) or abs(rho) < 0.9:
        return "its effect is not a simple up or down"
    return f"higher values go with {'higher' if rho > 0 else 'lower'} {what}"
