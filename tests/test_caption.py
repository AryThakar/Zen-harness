# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A picture whose name says nothing (a pasted image.png) is named from a caption the vision model writes; the caption
is kept and find searches it (caption.py)."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.providers import ScriptedProvider
from app.tools.caption import name_from_caption, says_nothing
from app.tools.files import find_files
from app.tools.search_index import SearchIndex
from app.tools.workspace import Workspace


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 12), "gold").save(out, "PNG")
    return out.getvalue()


class Looks:
    """A vision model that says what it is told to, and counts its calls."""
    def __init__(self, said: str | Exception = "A lion resting on a rock."):
        self.said, self.calls = said, 0

    def __call__(self, data: bytes, mime: str, question: str) -> str:
        self.calls += 1
        assert mime.startswith("image/") and "caption" in question
        if isinstance(self.said, Exception):
            raise self.said
        return self.said


@pytest.mark.parametrize("name, nothing", [("image.png", True), ("image (2).png", True), ("IMG_1234.jpg", True),
                                           ("Screenshot 2026-10-04 at 10.37.12.png", True), ("1234.png", True),
                                           ("panda.jpg", False), ("lion_photo.png", False), ("receipt.png", False)])
def test_which_names_say_nothing(name, nothing):
    assert says_nothing(name) is nothing


def test_a_pasted_picture_is_named_from_its_caption_and_found_by_it(tmp_path):
    ws, index = Workspace(tmp_path / "ws"), SearchIndex(tmp_path / "s.sqlite", None)
    key = ws.add_image(_png(), "image.png", by="user")
    look = Looks('Caption: "A lion resting on a rock."')
    assert name_from_caption(ws, key, look) == {"caption": "A lion resting on a rock",
                                                "name": "a-lion-resting-on-a-rock.png"}
    assert ws.label(key) == "a-lion-resting-on-a-rock.png" and ws.entry(key)["meta"]["caption"] == "A lion resting on a rock"
    second = ws.add_image(_png() + b"", "image.png", by="user", dedupe=False)
    assert name_from_caption(ws, second, Looks()) == {"caption": "A lion resting on a rock",
                                                      "name": "a-lion-resting-on-a-rock (2).png"}  # unique
    assert find_files(ws, index, "lion").result["found"] == 2  # by the caption


def test_a_real_name_stays_and_a_failed_look_keeps_the_name(tmp_path):
    ws = Workspace(tmp_path / "ws")
    named = ws.add_image(_png(), "panda.jpg", by="user")
    look = Looks()
    assert name_from_caption(ws, named, look) is None and look.calls == 0  # its name says what it is
    pasted = ws.add_image(_png(), "image.png", by="user", dedupe=False)
    assert name_from_caption(ws, pasted, Looks(RuntimeError("tunnel down"))) is None and ws.label(pasted) == "image.png"
    assert name_from_caption(ws, pasted, None) is None  # no vision


def test_an_upload_comes_back_with_its_new_name(tmp_path):
    from app.assistant.store import Store
    from app.main import create_app
    worker = ScriptedProvider([])
    worker.sees_images, worker.look = (lambda: True), Looks("A receipt from a coffee shop")
    with TestClient(create_app(assistant_parts=(Store(tmp_path / "a"), ScriptedProvider([]), worker))) as client:
        ws_id = client.post("/assistant/workspaces", json={"name": "w"}).json()["id"]
        r = client.post(f"/assistant/workspaces/{ws_id}/files", files={"file": ("image.png", _png(), "image/png")}).json()
    assert r["name"] == "a-receipt-from-a-coffee-shop.png" and r["caption"] == "A receipt from a coffee shop"
