"""analyze_text: what a text column is about - themes, the user's own labels, or the rows like a phrase.

    themes    texts grouped by meaning: local embeddings (the search index's model) and
              k-means, 3 to 8 themes chosen by silhouette unless given (the clearest split: unlike
              customer segments, two vague themes are no overview). Each theme comes with its share, its most telling words (class TF-IDF) and
              the texts closest to its centre, so the helper names it from evidence; with value, the
              average of a number per theme (rating by theme).
    classify  each text given the label it is closest to in meaning (zero-shot: the labels are
              embedded as 'This is about <label>.'); texts whose best and second-best labels are
              almost equally close are marked unsure (each model has its own gap: EmbeddingGemma 2's
              label gaps are about half of bge-small's, measured without labels). Measured on real
              names with known labels - pizza names -> 4 menu categories: EmbeddingGemma 2 56%,
              bge-small 53%; coffee products -> 9 categories: 45% and 29%; car names -> country
              (bge-small, held out) 70%. It works for labels that are about meaning, not a business's
              own codes; other ways of embedding the labels did no better (bge-small).
    similar   the rows closest in meaning to a phrase.

Repeated texts (product names, categories) are embedded once and weighted by how often they occur;
at most MAX_TEXTS distinct texts are used. Results are saved as a table (each row's theme or label)
for charts and queries. Without the embedding model, a word-based fallback (TF-IDF) is used and said.
Ported from the analyst project (analyst/ops/learn.py themes); classify and similar are new.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from app.rag.embed import EmbedderUnavailable

from .core import ToolError, ToolResult, number, ok, tool
from .roles import table_roles
from .workspace import Workspace, resolve_column

MAX_TEXTS = 5000
MIN_TEXTS = 20
MAX_THEMES = 8
UNSURE = 0.02  # cosine gap between the best and second-best label below which a text is marked unsure
               # (bge-small's; a model with another scale has its own unsure_gap - app/rag/embed.py)
EXAMPLE_CHARS = 160


@tool
def analyze_text(ws: Workspace, data: str, kind: Literal["themes", "classify", "similar"], column: str | None = None,
                 labels: list[str] | None = None, query: str | None = None, count: int | None = None,
                 value: str | None = None, embedder=None) -> ToolResult:
    """Themes, labels or similar rows of a text column."""
    df = ws.table(data)
    column = resolve_column(df, column, "column") if column else _text_column(ws, data, df)
    value = resolve_column(df, value, "value") if value else None
    texts = df[column].astype("string").str.strip()
    texts = texts[texts.notna() & (texts.str.len() > 2)]
    if texts.empty:
        raise ToolError(f"'{column}' has no text.", field="column", invalid=False)
    freq = texts.value_counts()
    notes = []
    if len(freq) > MAX_TEXTS:
        notes.append(f"The {MAX_TEXTS:,} most frequent of {len(freq):,} distinct texts are used.")
        freq = freq.head(MAX_TEXTS)
    unique = [str(t) for t in freq.index]
    vectors, method = _embed(unique, embedder)
    if kind == "themes":
        result, artifact, mapping = _themes(unique, freq.to_numpy(), vectors, count, column)
        new_col = "theme"
    elif kind == "classify":
        result, artifact, mapping = _classify(unique, freq.to_numpy(), vectors, labels, embedder, method, column)
        new_col = "label"
    else:
        result, artifact = _similar(df, texts, unique, freq, vectors, query, embedder, method, column)
        result.update({"kind": kind, "data": data, "column": column, "method": method})
        if notes:
            result["notes"] = notes
        return ok(result, artifact)
    out = df.copy()
    out[new_col] = texts.map(mapping)
    if value:
        if not pd.api.types.is_numeric_dtype(df[value]) or pd.api.types.is_bool_dtype(df[value]):
            raise ToolError(f"'{value}' is not a number column.", field="value")
        means = out.groupby(new_col)[value].mean()
        key = "theme" if kind == "themes" else "label"
        for g in result["themes" if kind == "themes" else "labels"]:
            g[f"average_{value}"] = number(float(means.get(g[key], np.nan)))
        result["overall_average"] = number(float(df[value].mean()))
    saved = ws.add_table(out, name=ws.derived_name(data, f"{new_col}s"), parent=data, meta={"text": column})
    result = {"kind": kind, "data": data, "column": column, "texts": int(freq.sum()), "distinct_texts": len(unique),
              "method": method, **result, "saved_as": saved,
              "saved_note": f"{saved} is {data} with a {new_col} column (rows without text have none)."}
    if notes:
        result["notes"] = notes
    return ok(result, artifact)


def _text_column(ws, data, df) -> str:
    role = table_roles(ws, data, df).get("text")
    if role:
        return role
    text = [str(c) for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])
            and not pd.api.types.is_datetime64_any_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    if len(text) == 1:  # the table's only text column is the one meant (measured: refused, though it named it)
        return text[0]
    raise ToolError("Give column: the text column to read" + (f" ({', '.join(text)})." if text else
                    "; this table has no text columns."), field="column")


def _embed(texts: list[str], embedder) -> tuple[np.ndarray, str]:
    if embedder is not None:
        try:
            return embedder.embed_documents(texts), f"meaning-based ({embedder.name} embeddings)"
        except EmbedderUnavailable:
            pass
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    tfidf = TfidfVectorizer(stop_words="english").fit_transform(texts)
    dims = max(2, min(100, tfidf.shape[1] - 1, len(texts) - 1))
    v = TruncatedSVD(dims, random_state=0).fit_transform(tfidf)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9), "word-based (TF-IDF; the embedding model was not available)"


def _themes(texts, weights, vectors, count, column):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    if len(texts) < MIN_TEXTS:
        raise ToolError(f"Only {len(texts)} distinct texts in {column}; themes need at least {MIN_TEXTS}.", invalid=False)
    tried = []
    sizes = [int(count)] if count else range(3, max(3, min(MAX_THEMES, len(texts) // 10)) + 1)
    for k in sizes:
        k = max(2, min(k, len(texts) - 1))
        labels = KMeans(k, n_init=10, random_state=0).fit_predict(vectors, sample_weight=weights)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(vectors, labels, metric="cosine", sample_size=min(2000, len(texts)), random_state=0))
        tried.append((score, k, labels))
    if not tried:
        raise ToolError("Could not form themes.", invalid=False)
    score, k, labels = max(tried, key=lambda t: t[0])
    order = pd.Series(weights).groupby(labels).sum().sort_values(ascending=False).index
    number_of = {g: i + 1 for i, g in enumerate(order)}
    words = _keywords(texts, weights, labels, order)
    total = weights.sum()
    themes, rows = [], []
    for g in order:
        mask = labels == g
        centre = vectors[mask].mean(axis=0)
        closest = np.argsort(-(vectors[mask] @ centre))[:3]
        examples = [texts[i][:EXAMPLE_CHARS] for i in np.where(mask)[0][closest]]
        share = float(weights[mask].sum() / total)
        themes.append({"theme": number_of[g], "share": number(share, 3), "rows": int(weights[mask].sum()),
                       "words": words[g], "examples": examples})
        rows.append([number_of[g], ", ".join(words[g]), int(weights[mask].sum()), number(share, 3), examples[0]])
    clarity = "distinct" if score >= 0.25 else "somewhat distinct" if score >= 0.1 else "overlapping"
    result = {"themes": themes, "separation": {"silhouette": number(score, 2), "meaning": f"{clarity} themes"},
              "naming": "The themes are numbered: name each from its words and examples."}
    artifact = {"type": "table", "title": f"Themes in {column}", "columns": ["theme", "words", "rows", "share", "example"],
                "rows": rows, "notes": [f"{len(themes)} themes, {clarity} (silhouette {score:.2f})."]}
    return result, artifact, dict(zip(texts, (number_of[g] for g in labels)))


def _keywords(texts, weights, labels, groups, top: int = 5) -> dict:
    """Each theme's most telling words: frequent in it, rare elsewhere (class TF-IDF)."""
    from sklearn.feature_extraction.text import CountVectorizer
    try:
        counts = CountVectorizer(stop_words="english", ngram_range=(1, 2), min_df=2, max_features=5000)
        m = counts.fit_transform(texts).multiply(weights[:, None]).tocsr()
        vocab = np.array(counts.get_feature_names_out())
    except ValueError:  # no words left (codes, numbers)
        return {g: [] for g in groups}
    freq_all = np.asarray(m.sum(axis=0)).ravel()
    avg = m.sum() / len(groups)
    out = {}
    for g in groups:
        tf = np.asarray(m[labels == g].sum(axis=0)).ravel()
        weight = tf * np.log(1 + avg / (freq_all + 1))
        out[g] = [str(vocab[i]) for i in np.argsort(-weight)[:top] if weight[i] > 0]
    return out


def _classify(texts, weights, vectors, labels, embedder, method, column):
    if not labels or len(labels) < 2:
        raise ToolError("Give labels: 2 to 12 categories to sort the texts into, e.g. ['delivery', 'price', "
                        "'quality'].", field="labels")
    labels = [str(lab).strip() for lab in labels if str(lab).strip()][:12]
    if method.startswith("word-based"):
        raise ToolError("Sorting texts by meaning needs the embedding model, which is not available; use query_data "
                        "with LIKE rules instead.", invalid=False)
    label_vectors = embedder.embed_documents([f"This is about {lab}." for lab in labels])
    sims = vectors @ label_vectors.T
    best = sims.argmax(axis=1)
    ordered = np.sort(sims, axis=1)
    gap = ordered[:, -1] - ordered[:, -2]
    unsure = gap < getattr(embedder, "unsure_gap", UNSURE)
    total = weights.sum()
    out, rows = [], []
    for j, lab in enumerate(labels):
        mask = best == j
        sure = mask & ~unsure
        idx = np.where(sure if sure.any() else mask)[0]
        examples = [texts[i][:EXAMPLE_CHARS] for i in idx[np.argsort(-sims[idx, j])][:3]]
        share = float(weights[mask].sum() / total)
        out.append({"label": lab, "rows": int(weights[mask].sum()), "share": number(share, 3), "examples": examples})
        rows.append([lab, int(weights[mask].sum()), number(share, 3), examples[0] if examples else ""])
    unsure_share = float(weights[unsure].sum() / total)
    result = {"labels": out, "unsure_share": number(unsure_share, 3),
              "caveat": "Matched by meaning with an embedding model - good on labels with a clear meaning, about half "
                        "right on a business's own categories (measured 45-56% on menus): check the examples. Texts "
                        "marked unsure were almost as close to another label; for exact rules use query_data with LIKE."}
    artifact = {"type": "table", "title": f"{column} by label", "columns": ["label", "rows", "share", "example"],
                "rows": rows, "notes": [f"{unsure_share:.0%} of the texts are unsure (two labels almost equally close)."]}
    mapping = {t: labels[b] for t, b in zip(texts, best)}
    return result, artifact, mapping


def _loose(embedder) -> float:
    """The similarity below which a match is loose: the model's own cut-off for search, which asks the same
    question (a phrase against texts) - 0.6 for bge-small, measured when this tool was made."""
    return getattr(embedder, "min_similarity", 0.6)


def _similar(df, texts, unique, freq, vectors, query, embedder, method, column):
    if not query or not query.strip():
        raise ToolError("Give query: the phrase to find texts like.", field="query")
    if method.startswith("word-based"):
        raise ToolError("Finding texts by meaning needs the embedding model, which is not available; use query_data "
                        "with LIKE instead.", invalid=False)
    q = embedder.embed_query(query)
    sims = vectors @ q
    top = np.argsort(-sims)[:10]
    first_row = texts.reset_index().drop_duplicates(subset=[texts.name]).set_index(texts.name)["index"]
    matches = [{"text": unique[i][:EXAMPLE_CHARS], "similarity": number(float(sims[i]), 3),
                "rows": int(freq.iloc[i]), "first_row": int(first_row.get(unique[i], -1)) + 1} for i in top]
    result = {"query": query, "matches": matches,
              "note": f"similarity is cosine (1 = same meaning); below about {_loose(embedder)} a match is loose."}
    artifact = {"type": "table", "title": f"{column} like '{query}'", "columns": ["text", "similarity", "rows"],
                "rows": [[m["text"], m["similarity"], m["rows"]] for m in matches]}
    return result, artifact
