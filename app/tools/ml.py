"""train_model and predict: learn a column from the others, then apply the model to new rows.

The model decides what to predict; this code decides how, the same way every time:
- task: yes/no or a few classes -> classification; a number -> regression
- inputs: the other columns, minus ids, free text, constants and dates (reported with reasons);
  month and weekday come from the date column when there is one
- models: a regularised linear model (logistic / ridge regression) and gradient boosting
  (scikit-learn HistGradientBoosting) are tested the same way and the better one is kept - on a few
  hundred rows boosting overfits and the linear model wins; with more data and non-linear
  patterns boosting does. The reported score is the winner's, so it is slightly optimistic.
- test: with a date column, learn from the earlier 80% of the time span and score on the latest
  20% (no peeking at the future); otherwise 5-fold cross-validation
- always against a baseline (the average / the most common class): the verdict says plainly when
  the model is not better
- a column that predicts the target almost perfectly on its own is reported as a likely leak
- important predictive features by permutation importance (associations, not causes)

Ported from the analyst project (analyst/ops/predict.py, column mode); the linear candidate is new.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .core import ToolError, ToolResult, number, ok, tool
from .workspace import Workspace, resolve_column

MIN_ROWS = 50
MAX_CLASSES = 10
TOP_CATEGORIES = 30
LEAK_SCORE = 0.98
MARGIN = 0.02  # how much better than the baseline counts as better


# ---------------------------------------------------------------- features

def _numeric(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _choose_features(df: pd.DataFrame, target: str, time_col: str | None, wanted: list[str] | None):
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


# ---------------------------------------------------------------- train

@tool
def train_model(ws: Workspace, data: str, target: str, features: list[str] | None = None,
                time_column: str | None = None) -> ToolResult:
    """Train and test a model that predicts ``target`` from the other columns; save it."""
    from sklearn.metrics import accuracy_score, mean_absolute_error, r2_score, roc_auc_score

    df = ws.table(data)
    target = resolve_column(df, target, "target")
    wanted = [resolve_column(df, c, "features") for c in features] if features else None
    time_col = _time_column(df, time_column)
    frame = df.dropna(subset=[target]).reset_index(drop=True)
    if len(frame) < MIN_ROWS:
        raise ToolError(f"Only {len(frame)} rows have {target}; training needs at least {MIN_ROWS}.", invalid=False)

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

    cols, excluded = _choose_features(frame, target, time_col, wanted)
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
            preds[name].append(predict_scores(m, X.loc[test]))
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
    scores = {name: evaluate(y_test, np.concatenate(p)) for name, p in preds.items()}
    chosen = max(scores, key=scores.get)
    p_all, score = np.concatenate(preds[chosen]), scores[chosen]

    extra = {}
    if binary:
        metric, base = "AUC", 0.5
    elif classify:
        metric, base = "accuracy", float(accuracy_score(y_test, b_all))
    else:
        metric, base = "R2", float(r2_score(y_test, b_all))
        extra = {"average_error": number(float(mean_absolute_error(y_test, p_all))),
                 "baseline_average_error": number(float(mean_absolute_error(y_test, b_all)))}
    better = score > base + MARGIN
    verdict = _verdict(metric, score, better)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        full = candidates[chosen]().fit(X, y)
    important = _importance(full, X, y, binary, positive) if better else []
    leaks = _leaks(X, y, classify, binary, positive)

    meta = {"target": target, "task": "classification" if classify else "regression", "metric": metric,
            "score": number(score), "baseline": number(base), "better_than_baseline": better, "tested_on": tested_on,
            "model_type": chosen, "scores_by_model": {k: number(v) for k, v in scores.items()},
            "features": list(X.columns), "positive_class": positive, "classes": classes}
    bundle = {"model": full, "spec": spec, "target": target, "classify": classify, "binary": binary,
              "positive": positive}
    model_id = ws.add_model(bundle, name=f"{target} model", parent=data, meta=meta)
    result = {"model": model_id, "data": data, "target": target, "task": meta["task"], "rows": len(frame),
              "tested_on": tested_on, "model_type": chosen, "metric": metric, "score": number(score),
              "baseline": number(base), **extra, "scores_by_model": meta["scores_by_model"],
              "better_than_baseline": better, "verdict": verdict,
              "important_predictive_features": [{"column": c, "share": number(v, 3)} for c, v in important],
              "excluded_columns": excluded}
    if binary:
        result["positive_class"] = positive
    if leaks:
        result["warning"] = (f"{', '.join(leaks)} predict {target} almost perfectly on their own: check they are not "
                             "recorded after the outcome (a leak). If so, train again without them.")
    if important:
        result["note"] = "Important features are associated with the outcome; they are not proven causes."
    artifact = {"type": "table", "title": f"{model_id}: predicting {target}", "columns": ["column", "importance"],
                "rows": [[c, number(v, 3)] for c, v in important], "notes": [verdict]}
    return ok(result, artifact)


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
    from scipy import stats
    from sklearn.metrics import roc_auc_score
    if classify and not binary:
        return []
    out = []
    for c in X.columns:
        x = X[c]
        if str(x.dtype) == "category":
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


def _importance(model, X, y, binary, positive, top: int = 5) -> list[tuple[str, float]]:
    """Permutation importance: how much the score drops when one column is shuffled."""
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
    return [(c, float(v)) for c, v in imp.sort_values(ascending=False).head(top).items() if v >= 0.03]


# ---------------------------------------------------------------- predict

@tool
def predict(ws: Workspace, model: str, data: str) -> ToolResult:
    """Apply a trained model to a table; save the table with the predictions as a new table."""
    bundle = ws.model(model)
    meta = ws.entry(model)["meta"]
    df = ws.table(data)
    X, missing = _build(df, bundle["spec"])
    if len(missing) == len(bundle["spec"]["columns"]) + bool(bundle["spec"]["time_features"]):
        raise ToolError(f"{data} has none of the columns {model} learned from "
                        f"({', '.join(meta['features'])}).", invalid=False)
    m, target = bundle["model"], bundle["target"]
    out = df.copy()
    col = f"predicted_{target}"
    if bundle["binary"]:
        proba = m.predict_proba(X)[:, list(m.classes_).index(bundle["positive"])]
        out[f"probability_{bundle['positive']}"] = proba.round(4)
        out[col] = m.predict(X)
        summary = {"average_probability": number(float(proba.mean())),
                   "predicted_counts": out[col].value_counts().to_dict()}
    elif bundle["classify"]:
        out[col] = m.predict(X)
        summary = {"predicted_counts": out[col].value_counts().to_dict()}
    else:
        out[col] = m.predict(X)
        summary = {"average_prediction": number(float(out[col].mean())),
                   "min": number(float(out[col].min())), "max": number(float(out[col].max()))}
    new_id = ws.add_table(out, name=f"{ws.entry(data)['name']} + {target} predictions", parent=data,
                          meta={"model": model})
    result = {"data": new_id, "from": data, "model": model, "rows": len(out), "column": col, **summary,
              "model_quality": f"{meta['metric']} {meta['score']} vs {meta['baseline']} baseline, tested on {meta['tested_on']}"}
    if missing:
        result["warning"] = f"{data} lacks {', '.join(missing)}: predicted as if unknown, so less accurate."
    if not meta["better_than_baseline"]:
        result["warning"] = (result.get("warning", "") + " The model was not better than the baseline: these "
                             "predictions are no more useful than the average.").strip()
    preview = out.head(20).astype(object).where(out.head(20).notna(), None)
    return ok(result, {"type": "table", "title": f"{ws.describe(new_id)}", "columns": list(map(str, out.columns)),
                       "rows": preview.values.tolist()})
