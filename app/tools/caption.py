"""A picture that arrives with a name that says nothing - a pasted image.png, IMG_1234.jpg, a screenshot - is named from a
one-line caption the vision model writes, and the caption is kept on it, where find searches it (docs/history/REBUILD_PLAN.md
2.1: eight uploads were all called image.png, and "which is the receipt?" had nothing to go on).

It runs while the picture uploads, before the message that brings it is sent - so the model never sees a rename in the
middle of a turn. Only pictures with names that say nothing are captioned there: a caption is a call to the model
(3-4 s), and a picture with a real name is found by its name and, when the embedding model takes pictures, by what it
shows. Without vision, or when the model can't be reached, the picture keeps its name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import PurePosixPath

from .vision import prepare
from .workspace import Workspace

log = logging.getLogger(__name__)

ASK = ("Write a caption of what this picture shows - one line, at most 12 words, no full stop at the end. Answer with "
       "the caption only.")
# Words a name holds when it says nothing about the picture (with digits, dates and times around them)
GENERIC_WORDS = {"image", "img", "photo", "picture", "pic", "screenshot", "screen", "shot", "pasted", "untitled",
                 "file", "download", "clipboard", "capture", "scan", "dsc", "pxl", "whatsapp", "mmexport", "copy", "new"}
SLUG_WORDS = 7
MAX_CAPTION = 120

Look = Callable[[bytes, str, str], str]


def says_nothing(name: str) -> bool:
    """Whether a picture's name tells nothing about it: 'image (2).png', 'IMG_1234.jpg', 'Screenshot 2026-10-04 at
    10.37.12.png', '1234.png' - no word of three letters or more besides the generic ones."""
    return all(w in GENERIC_WORDS for w in re.findall(r"[a-z]{3,}", PurePosixPath(name).stem.lower()))


def name_from_caption(ws: Workspace, key: str, look: Look | None) -> dict | None:
    """Caption a picture whose name says nothing and name it from the caption; {'caption', 'name'}, or None when it
    keeps its name (a real name, no vision, the model unreachable)."""
    entry = ws.entry(key)
    if entry["kind"] != "image" or look is None or not says_nothing(entry["name"]):
        return None
    data, mime = prepare(ws.path(key).read_bytes())
    try:
        said = look(data, mime, ASK)
    except Exception:  # noqa: BLE001 - an upload never fails for its caption
        log.exception("Could not caption %s", entry["name"])
        return None
    caption = _clean(said)
    words = re.findall(r"[a-z0-9]+", caption.lower())[:SLUG_WORDS]
    if not words:
        return None
    ws.set_meta(key, "caption", caption)
    old = PurePosixPath(entry["name"])
    folder = "" if str(old.parent) in ("", ".") else f"{old.parent}/"
    new = ws.free_path(f"{folder}{'-'.join(words)}{old.suffix.lower()}", own=key)
    ws.rename(key, new)
    return {"caption": caption, "name": new}


def _clean(said: str) -> str:
    """The caption as one plain line: its first line, without quotes, a label or the full stop."""
    line = next((ln.strip() for ln in str(said or "").splitlines() if ln.strip()), "")
    line = re.sub(r"^(?:caption\s*:\s*)", "", line, flags=re.I).strip(" \"'*`").rstrip(".")
    return line[:MAX_CAPTION]
