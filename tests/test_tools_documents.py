"""Finished documents: every chart kept as a picture (chart_image.py), and Word and slide files written from
Markdown with those pictures in them (file_ops.py) - opened again with python-docx and python-pptx."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest
from docx import Document
from PIL import Image
from pptx import Presentation

from app.rag.embed import HashEmbedder
from app.tools import chart_image
from app.tools.file_ops import files
from app.tools.find_patterns import find_patterns
from app.tools.forecast import forecast
from app.tools.make_chart import make_chart
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def index(tmp_path):
    return SearchIndex(tmp_path / "index.sqlite", HashEmbedder())


@pytest.fixture
def sales(ws):
    rng = np.random.default_rng(0)
    days = pd.date_range("2023-01-01", periods=730, freq="D")
    df = pd.DataFrame({"date": days, "region": rng.choice(["North", "South", "East"], len(days)),
                       "amount": rng.gamma(2.0, 50.0, len(days)).round(2), "units": rng.integers(1, 9, len(days))})
    return ws.add_table(df, "sales.csv")


def _picture(ws, item):
    with Image.open(ws.path(item)) as img:
        return img.format, img.size


@pytest.mark.parametrize("kind, args", [
    ("line", {"x": "date", "y": "amount"}), ("line", {"x": "date", "y": "amount", "by": "region"}),
    ("bar", {"x": "region", "y": "amount"}), ("bar", {"x": "region", "y": "amount", "by": "units", "stacked": True}),
    ("scatter", {"x": "units", "y": "amount", "by": "region"}), ("histogram", {"x": "amount"}),
    ("box", {"x": "region", "y": "amount"}), ("heatmap", {"x": "region", "by": "units"}), ("pie", {"x": "region", "y": "amount"})])
def test_every_chart_is_also_kept_as_a_picture(ws, sales, kind, args):
    r = make_chart(ws, sales, kind, **args)
    assert r.status == "ok" and ws.kind(r.result["picture"]) == "image"
    assert _picture(ws, r.result["picture"]) == ("PNG", (960, 540))
    assert ws.entry(r.result["picture"])["name"].endswith(".png") and ws.entry(r.result["picture"])["parent"] == sales


def test_forecasts_and_unusual_periods_are_kept_as_pictures_too(ws, sales):
    assert _picture(ws, forecast(ws, sales, value="amount", time_unit="month").result["picture"])[0] == "PNG"
    assert _picture(ws, forecast(ws, sales, value="amount", time_unit="month", by="region").result["picture"])[0] == "PNG"
    assert _picture(ws, find_patterns(ws, sales, "unusual", value="amount").result["picture"])[0] == "PNG"


def test_a_chart_that_cannot_be_drawn_has_no_picture_and_still_shows(ws, monkeypatch):
    monkeypatch.setattr(chart_image, "draw", lambda a: 1 / 0)
    assert chart_image.keep_chart(ws, {"chart": "line", "title": "t"}) is None


def test_a_word_document_includes_a_chart(ws, index, sales):
    chart = make_chart(ws, sales, "bar", x="region", y="amount").result["picture"]
    r = files(ws, index, "write", name="report.docx",
              content=f"# Sales report\n\nThe **North** leads.\n\n![Sales by region]({chart})\n\n![Missing](image_99)\n")
    assert r.status == "ok"
    doc = Document(io.BytesIO(ws.path(r.result["file"]).read_bytes()))
    assert len(doc.inline_shapes) == 1 and any(p.text == "Sales by region" for p in doc.paragraphs)
    assert any("[picture image_99 not found]" in p.text for p in doc.paragraphs)


def test_slides_are_made_from_markdown_with_charts_and_notes(ws, index, sales):
    chart = make_chart(ws, sales, "line", x="date", y="amount").result["picture"]
    deck = (f"# Q4 sales review\nPrepared by Zen\n\n"
            f"# Highlights\n- Revenue grew **12%**\n  - mostly in the North\n- Fewer returns\n"
            f"![Revenue by month]({chart})\nNotes: start with the good news\n\n"
            f"# The trend\n![Revenue by month]({ws.entry(chart)['name']})\n\n## Next steps\n1. Hire\n2. Expand\n")
    r = files(ws, index, "write", name="review.pptx", content=deck)
    assert r.status == "ok" and r.result["kind"] == "file"
    prs = Presentation(io.BytesIO(ws.path(r.result["file"]).read_bytes()))
    slides = list(prs.slides)
    assert [s.shapes.title.text for s in slides] == ["Q4 sales review", "Highlights", "The trend", "Next steps"]
    assert slides[0].placeholders[1].text == "Prepared by Zen"  # a title slide with its subtitle
    pictures = [sum(sh.shape_type == 13 for sh in s.shapes) for s in slides]  # 13: a picture
    assert pictures == [0, 1, 1, 0]
    text = " ".join(sh.text_frame.text for sh in slides[1].shapes if sh.has_text_frame)
    assert "Revenue grew 12%" in text and "mostly in the North" in text
    bold = [run for sh in slides[1].shapes if sh.has_text_frame for p in sh.text_frame.paragraphs for run in p.runs
            if run.text == "12%"]
    assert bold and bold[0].font.bold
    assert slides[1].notes_slide.notes_text_frame.text == "start with the good news"
    assert all(sh.left + sh.width <= prs.slide_width for s in slides for sh in s.shapes)  # nothing off the slide


def test_slides_need_a_heading_and_are_changed_whole(ws, index):
    assert files(ws, index, "write", name="empty.pptx", content="\n\n").status == "invalid_arguments"
    r = files(ws, index, "write", name="deck.pptx", content="# One\n- a\n")
    assert files(ws, index, "update", name="deck.pptx", content="- b", start=2, end=2).status == "invalid_arguments"
    assert files(ws, index, "update", name="deck.pptx", content="# One\n- b\n# Two\n").status == "ok"
    assert len(Presentation(io.BytesIO(ws.path(r.result["file"]).read_bytes())).slides) == 2
