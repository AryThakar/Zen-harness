# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The theme picked in the page, kept where Zen-harness.exe reads it to open its window's title bar to match."""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app.api import assistant as assistant_api
from app.assistant.store import Store
from app.main import create_app
from app.providers import ScriptedProvider
from app.rag.embed import HashEmbedder


def test_the_window_learns_the_picked_theme(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant_api, "settings", dataclasses.replace(assistant_api.settings, assistant_dir=str(tmp_path / "assistant")))
    store = Store(tmp_path / "store", HashEmbedder())
    with TestClient(create_app(assistant_parts=(store, ScriptedProvider([]), ScriptedProvider([])))) as c:
        assert c.put("/assistant/window-theme", json={"theme": "light"}).status_code == 204
        assert (tmp_path / "assistant" / "window-theme").read_text(encoding="utf-8") == "light"
        assert c.put("/assistant/window-theme", json={"theme": "dark"}).status_code == 204
        assert (tmp_path / "assistant" / "window-theme").read_text(encoding="utf-8") == "dark"
        assert c.put("/assistant/window-theme", json={"theme": "purple"}).status_code == 422
    store.close()
