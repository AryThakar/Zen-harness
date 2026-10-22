# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Split extracted blocks into passages for search.

A passage is about TARGET_WORDS words, made of whole paragraphs where possible
(long paragraphs are split at sentence ends), never crosses a PDF page or a
heading, and repeats the last OVERLAP_WORDS words of the previous passage in the
same section so a sentence cut at the boundary is still found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import Block

TARGET_WORDS = 250
MAX_WORDS = 350
OVERLAP_WORDS = 40


@dataclass
class Chunk:
    text: str
    page: int | None
    section: str | None


MIN_WORDS = 20  # a shorter passage (a lone title or heading) is merged into the next one


def chunk(blocks: list[Block]) -> list[Chunk]:
    chunks: list[Chunk] = []
    group: list[Block] = []
    for block in blocks:
        if group and (block.page, block.section) != (group[0].page, group[0].section):
            chunks += _chunk_group(group)
            group = []
        group.append(block)
    if group:
        chunks += _chunk_group(group)
    return _merge_short(chunks)


def _merge_short(chunks: list[Chunk]) -> list[Chunk]:
    out: list[Chunk] = []
    carry: Chunk | None = None
    for c in chunks:
        if carry is not None and carry.page == c.page:
            c = Chunk(f"{carry.text} {c.text}", c.page, c.section)
        elif carry is not None:
            out.append(carry)
        carry = None
        if len(c.text.split()) < MIN_WORDS:
            carry = c
        else:
            out.append(c)
    if carry is not None:
        out.append(carry)
    return out


def _chunk_group(blocks: list[Block]) -> list[Chunk]:
    """Passages from paragraphs that share a page and a section."""
    page, section = blocks[0].page, blocks[0].section
    pieces = [piece for b in blocks for piece in _split_long(b.text)]
    out: list[Chunk] = []
    current: list[str] = []
    words = 0
    for piece in pieces:
        n = len(piece.split())
        if current and words + n > TARGET_WORDS:
            out.append(Chunk(" ".join(current), page, section))
            tail = " ".join(" ".join(current).split()[-OVERLAP_WORDS:])
            current, words = [tail], len(tail.split())
        current.append(piece)
        words += n
    if current and (not out or words > OVERLAP_WORDS):  # skip a leftover that is only overlap
        out.append(Chunk(" ".join(current), page, section))
    return out


def _split_long(text: str) -> list[str]:
    """A paragraph longer than MAX_WORDS, split at sentence ends (or hard, if it has none)."""
    if len(text.split()) <= MAX_WORDS:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces, current = [], []
    for sentence in sentences:
        words = sentence.split()
        while len(words) > MAX_WORDS:  # one enormous "sentence": cut it
            if current:
                pieces.append(" ".join(current))
                current = []
            pieces.append(" ".join(words[:MAX_WORDS]))
            words = words[MAX_WORDS:]
        if current and len(" ".join(current).split()) + len(words) > TARGET_WORDS:
            pieces.append(" ".join(current))
            current = []
        current += words
    if current:
        pieces.append(" ".join(current))
    return pieces
