# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The model's connection, changed from the app: written in place, tried first, refused from other sites, and taken
by the bridge on its next request."""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import model_link
from app.api import assistant as assistant_api
from app.assistant.store import Store
from app.main import create_app
from app.providers import ScriptedProvider
from app.rag.embed import HashEmbedder

ROOT = Path(__file__).resolve().parents[1]


def test_the_file_is_changed_in_place(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("# what the notebook printed\nKAGGLE_URL=https://old.trycloudflare.com\nKAGGLE_PASSWORD=old\n"
                    "ASSISTANT_USER_NAME=Ary\n", encoding="utf-8")
    model_link.write(path, "https://new.trycloudflare.com", "secret")
    assert path.read_text(encoding="utf-8") == ("# what the notebook printed\nKAGGLE_URL=https://new.trycloudflare.com\n"
                                                "KAGGLE_PASSWORD=secret\nASSISTANT_USER_NAME=Ary\n")
    fresh = tmp_path / "none" / "bridge.env"
    model_link.write(fresh, "https://a.trycloudflare.com", "p")
    assert model_link.read(fresh) == {"KAGGLE_URL": "https://a.trycloudflare.com", "KAGGLE_PASSWORD": "p"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "bridge.env"
    path.write_text("KAGGLE_URL=https://old.trycloudflare.com\nKAGGLE_PASSWORD=old\n", encoding="utf-8")
    monkeypatch.setattr(assistant_api, "settings", dataclasses.replace(assistant_api.settings, model_settings=str(path)))
    store = Store(tmp_path / "assistant", HashEmbedder())
    with TestClient(create_app(assistant_parts=(store, ScriptedProvider([]), ScriptedProvider([])))) as c:
        yield c, path
    store.close()


def test_the_settings_window_never_gets_the_password(client):
    c, _ = client
    assert c.get("/assistant/model-link").json() == {"url": "https://old.trycloudflare.com", "password_set": True}


def test_a_new_address_is_tried_then_saved_and_a_wrong_one_is_not(client, monkeypatch):
    c, path = client
    tried = []
    monkeypatch.setattr(model_link, "check", lambda url, pw: (tried.append((url, pw)) or (pw == "right", "Qwen.gguf" if pw == "right" else "The password is wrong")))
    wrong = c.post("/assistant/model-link", json={"url": "https://new.trycloudflare.com/", "password": "nope"})
    assert wrong.status_code == 400 and "password is wrong" in wrong.json()["detail"]
    assert model_link.read(path)["KAGGLE_URL"] == "https://old.trycloudflare.com"  # nothing saved
    ok = c.post("/assistant/model-link", json={"url": "https://new.trycloudflare.com/", "password": "right"})
    assert ok.json() == {"ok": True, "model": "Qwen.gguf"}
    assert model_link.read(path) == {"KAGGLE_URL": "https://new.trycloudflare.com", "KAGGLE_PASSWORD": "right"}
    # an empty password keeps the saved one (the window doesn't show it)
    assert c.post("/assistant/model-link", json={"url": "https://third.trycloudflare.com", "password": ""}).status_code == 200
    assert tried[-1] == ("https://third.trycloudflare.com", "right")


def test_another_site_cannot_change_where_the_model_runs(client, monkeypatch):
    c, path = client
    monkeypatch.setattr(model_link, "check", lambda url, pw: (True, "x"))
    r = c.post("/assistant/model-link", json={"url": "https://evil.example", "password": "x"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and model_link.read(path)["KAGGLE_URL"] == "https://old.trycloudflare.com"
    bad = c.post("/assistant/model-link", json={"url": "ftp://x", "password": "x"})
    assert bad.status_code == 400 and "https://" in bad.json()["detail"]
    # a second line can't ride in with the password
    sneaky = c.post("/assistant/model-link", json={"url": "https://new.trycloudflare.com", "password": "x\nBIZ_LLM_PROVIDER=openai"})
    assert sneaky.status_code == 400 and "BIZ_LLM_PROVIDER" not in path.read_text(encoding="utf-8")


def test_the_notebooks_printed_lines_paste_as_they_are(client, monkeypatch):
    c, path = client
    monkeypatch.setattr(model_link, "check", lambda url, pw: (True, "Qwen.gguf"))
    r = c.post("/assistant/model-link", json={"url": "  address : https://new.trycloudflare.com/", "password": "password: right"})
    assert r.status_code == 200
    assert model_link.read(path) == {"KAGGLE_URL": "https://new.trycloudflare.com", "KAGGLE_PASSWORD": "right"}


def test_the_bridge_takes_a_changed_file_on_its_next_request(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    path.write_text("KAGGLE_URL=https://one.trycloudflare.com\nKAGGLE_PASSWORD=a\n", encoding="utf-8")
    monkeypatch.setenv("ZEN_MODEL_SETTINGS", str(path))
    monkeypatch.delenv("KAGGLE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["bridge.py"])
    spec = importlib.util.spec_from_file_location("bridge_under_test", ROOT / "kaggle" / "bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert bridge.Link.current() == ("https://one.trycloudflare.com", "a")
    model_link.write(path, "https://two.trycloudflare.com", "b")
    os.utime(path, (time.time() + 5, time.time() + 5))  # a new time stamp, however fast the write was
    assert bridge.Link.current() == ("https://two.trycloudflare.com", "b")
