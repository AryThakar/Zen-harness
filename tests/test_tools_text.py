# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Direct tests of analyze_text with the word-hashing test embedder - no model involved. (Its quality with the
real embedding model is measured in the module docstring.)"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.rag.embed import HashEmbedder
from app.tools.analyze_text import analyze_text
from app.tools.workspace import Workspace

# Seven phrases per topic, each from the topic's own words: the word-hashing test embedder sees words, not meaning.
TOPICS = {"delivery": ["parcel arrived late", "courier lost parcel", "delivery took weeks", "late delivery again",
                       "parcel delivery late", "courier delivery delayed", "delayed parcel courier"],
          "price": ["too expensive price", "price went up", "cheaper elsewhere expensive", "price is too high",
                    "expensive price increase", "overpriced expensive", "high price cheaper"],
          "quality": ["broke after use", "poor quality stitching", "quality poor broke", "fell apart poor quality",
                      "stitching broke apart", "poor stitching quality", "broke apart quality"]}


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


def reviews(n=240, seed=0):
    rng = np.random.default_rng(seed)
    topic = rng.choice(list(TOPICS), n)
    text = [rng.choice(TOPICS[t]) for t in topic]
    rating = np.where(topic == "price", 3, np.where(topic == "delivery", 2, 1)) + rng.integers(0, 2, n)
    return pd.DataFrame({"review": text, "rating": rating, "topic": topic})


def test_themes_group_texts_and_save_each_rows_theme(ws):
    data = ws.add_table(reviews(), "reviews")
    r = analyze_text(ws, data, "themes", column="review", count=3, value="rating", embedder=HashEmbedder())
    assert r.status == "ok", r.result
    res = r.result
    assert len(res["themes"]) == 3 and abs(sum(t["share"] for t in res["themes"]) - 1) < 0.01
    saved = ws.table(res["saved_as"])
    purity = saved.groupby("theme")["topic"].agg(lambda s: s.value_counts(normalize=True).iloc[0])
    assert purity.min() > 0.8  # each theme is mostly one topic
    assert all("average_rating" in t and t["words"] for t in res["themes"])


def test_classify_and_similar(ws):
    data = ws.add_table(reviews(), "reviews")
    r = analyze_text(ws, data, "classify", column="review", labels=["delivery", "price", "quality"],
                     embedder=HashEmbedder())
    saved = ws.table(r.result["saved_as"])
    assert set(saved["label"]) <= {"delivery", "price", "quality"} and "check the examples" in r.result["caveat"]
    r = analyze_text(ws, data, "similar", column="review", query="parcel arrived late", embedder=HashEmbedder())
    assert r.result["column"] == "review" and r.result["matches"][0]["text"] == "parcel arrived late"
    assert r.result["matches"][0]["first_row"] >= 1


def test_a_tables_only_text_column_is_read_without_being_named(ws):
    data = ws.add_table(reviews().drop(columns=["topic"]), "reviews")
    r = analyze_text(ws, data, "themes", count=3, embedder=HashEmbedder())
    assert r.status == "ok" and r.result["column"] == "review"


def test_text_tool_mistakes_say_what_to_give(ws):
    data = ws.add_table(reviews(), "reviews")
    labels_only = ws.add_table(pd.DataFrame({"code": ["a1", "b2"] * 20, "n": range(40)}), "codes")
    message = analyze_text(ws, labels_only, "themes", embedder=HashEmbedder()).result["error"]["message"]
    assert message == "'code' has no text."  # its only text column is read: short codes, not text
    two = ws.add_table(pd.DataFrame({"code": ["a1", "b2"] * 20, "note": ["x", "y"] * 20}), "two")
    message = analyze_text(ws, two, "themes", embedder=HashEmbedder()).result["error"]["message"]
    assert message == "Give column: the text column to read (code, note)."
    r = analyze_text(ws, data, "classify", column="review", labels=["one"], embedder=HashEmbedder())
    assert "Give labels" in r.result["error"]["message"]
    r = analyze_text(ws, data, "similar", column="review", embedder=HashEmbedder())
    assert "Give query" in r.result["error"]["message"]
