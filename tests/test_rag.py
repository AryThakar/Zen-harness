# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Documents for search: extraction and chunking (app/rag); the ranking is the search index's
(tests/test_tools_search.py)."""

import pytest

from app.rag.chunking import MAX_WORDS, OVERLAP_WORDS, chunk
from app.rag.extract import Block, DocumentError, extract
from tests.documents import pdf, samples

SAMPLES = samples()


# --- extraction --------------------------------------------------------------------

def test_extracts_every_format_with_pages_and_sections():
    md = extract("returns_and_warranty_policy.md", SAMPLES["returns_and_warranty_policy.md"])
    assert md.kind == "md" and md.title == "Returns, refunds and warranty policy"
    assert {b.section for b in md.blocks} >= {"Returns", "Refunds", "Warranty"}
    assert not any(b.text.startswith("#") for b in md.blocks)

    docx = extract("discount_and_pricing_policy.docx", SAMPLES["discount_and_pricing_policy.docx"])
    assert docx.kind == "docx" and docx.title == "Discount and pricing policy"
    assert "Smartphone X1 | 18,999 | Electronics" in [b.text for b in docx.blocks]  # table rows kept
    assert next(b for b in docx.blocks if b.text.startswith("Smartphone X1")).section == "Price list"

    pdf = extract("supplier_terms_smarttech.pdf", SAMPLES["supplier_terms_smarttech.pdf"])
    assert pdf.kind == "pdf" and pdf.pages == 2
    assert any("Priya Nair" in b.text and b.page == 2 for b in pdf.blocks)

    txt = extract("operations_log.txt", SAMPLES["operations_log.txt"])
    assert txt.kind == "txt" and txt.words > 100


def test_text_files_in_windows_encoding_are_read():
    doc = extract("notes.txt", "Café opening hours: 10–8".encode("cp1252"))
    assert doc.blocks[0].text == "Café opening hours: 10–8"


@pytest.mark.parametrize("name, data, message", [
    ("sheet.xlsx", b"PK...", "aren't supported"),
    ("empty.txt", b"", "empty"),
    ("blank.txt", b"   \n\n  ", "No text found"),
    ("scan.pdf", pdf([""]), "scanned PDF"),
    ("broken.pdf", b"%PDF-1.4 garbage", "couldn't be read"),
    ("broken.docx", b"not a zip", "save it as .docx"),
])
def test_unusable_files_are_explained(name, data, message):
    with pytest.raises(DocumentError, match=message):
        extract(name, data)


# --- chunking ----------------------------------------------------------------------

def test_long_sections_split_with_overlap_and_pages_never_mix():
    sentence = "The store opens at ten and closes at eight on weekdays. "
    blocks = [Block(sentence * 120, page=1), Block("Short page two text. " * 10, page=2)]
    chunks = chunk(blocks)
    page_one = [c for c in chunks if c.page == 1]
    assert len(page_one) >= 3 and all(len(c.text.split()) <= MAX_WORDS + OVERLAP_WORDS for c in page_one)
    first_tail = page_one[0].text.split()[-OVERLAP_WORDS:]
    assert page_one[1].text.split()[:OVERLAP_WORDS] == first_tail
    assert [c.page for c in chunks][-1] == 2 and "store opens" not in chunks[-1].text


def test_a_lone_heading_joins_the_next_passage():
    blocks = [Block("Delivery", section="Delivery"),
              Block("We deliver in Mumbai within two days. " * 5, section="Delivery details")]
    chunks = chunk(blocks)
    assert len(chunks) == 1 and chunks[0].text.startswith("Delivery We deliver")
