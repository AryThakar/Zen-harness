# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Zen's Model settings - Kaggle, this computer or a cloud - changed from the app: written in place, tried first,
refused from other sites, and taken by the app on its next request, the search model with them."""

from __future__ import annotations

import dataclasses
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import connection, model_link
from app.api import assistant as assistant_api
from app.assistant.store import Store
from app.connection import Connection, Live, LiveEmbedder
from app.main import create_app
from app.providers import ScriptedProvider
from app.rag.embed import HashEmbedder, NoEmbedder, ServerEmbedder

OLD = "KAGGLE_URL=https://old.trycloudflare.com\nKAGGLE_PASSWORD=old\n"


def test_the_file_is_changed_in_place(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("# mine\n" + OLD + "ASSISTANT_USER_NAME=Ary\n", encoding="utf-8")
    model_link.write(path, {"KAGGLE_URL": "https://new.trycloudflare.com", "ZEN_MODEL": "kaggle"})
    assert path.read_text(encoding="utf-8") == ("# mine\nKAGGLE_URL=https://new.trycloudflare.com\nKAGGLE_PASSWORD=old\n"
                                                "ASSISTANT_USER_NAME=Ary\nZEN_MODEL=kaggle\n")
    fresh = tmp_path / "none" / "model.env"
    model_link.write(fresh, {"LOCAL_URL": "http://127.0.0.1:8080"})
    assert model_link.read(fresh) == {"LOCAL_URL": "http://127.0.0.1:8080"}


def test_the_choice_in_use_and_what_each_one_needs():
    assert model_link.in_use({}) == "local" and model_link.in_use({"KAGGLE_URL": "https://x"}) == "kaggle"
    assert model_link.in_use({"ZEN_MODEL": "cloud", "KAGGLE_URL": "https://x"}) == "cloud"
    assert not model_link.ready("kaggle", {"KAGGLE_URL": "https://x"}) and model_link.ready("local", {})
    assert model_link.ready("cloud", {"CLOUD_URL": "http://127.0.0.1:11434/v1", "CLOUD_MODEL": "llama3.2"})  # no key


def test_a_zen_code_is_the_address_and_password_in_one_line():
    code = model_link.make_code("https://a-b.trycloudflare.com", "pass/word=")
    assert code.startswith("zen1.") and "pass" not in code
    assert model_link.code(code) == ("https://a-b.trycloudflare.com", "pass/word=")
    assert model_link.code(f"  Zen code: {code} ") == ("https://a-b.trycloudflare.com", "pass/word=")
    assert model_link.code("https://a-b.trycloudflare.com") is None and model_link.code("zen1.!!") is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "model.env"
    path.write_text(OLD, encoding="utf-8")
    monkeypatch.setattr(assistant_api, "settings", dataclasses.replace(assistant_api.settings, model_settings=str(path)))
    store = Store(tmp_path / "assistant", HashEmbedder())
    with TestClient(create_app(assistant_parts=(store, ScriptedProvider([]), ScriptedProvider([])))) as c:
        yield c, path
    store.close()


@pytest.fixture
def tried(monkeypatch):
    """Each check's (choice, values); a check passes unless the password or key is "wrong"."""
    calls = []

    def check(kind, values, timeout=20):
        calls.append((kind, dict(values)))
        if "wrong" in (values.get("KAGGLE_PASSWORD"), values.get("CLOUD_KEY")):
            return False, "The password is wrong: copy it again from the notebook's last cell."
        return True, {"kaggle": "Qwen.gguf", "local": "local.gguf", "cloud": values.get("CLOUD_MODEL")}[kind]

    monkeypatch.setattr(model_link, "check", check)
    return calls


def test_the_settings_window_never_gets_a_password_or_key(client):
    c, path = client
    model_link.write(path, {"CLOUD_URL": "https://api.groq.com/openai/v1", "CLOUD_KEY": "gsk_secret", "CLOUD_MODEL": "m"})
    got = c.get("/assistant/model-settings").json()
    assert got["use"] == "kaggle" and got["kaggle"] == {"url": "https://old.trycloudflare.com", "password_set": True,
                                                         "ready": True}
    assert got["cloud"] == {"url": "https://api.groq.com/openai/v1", "model": "m", "key_set": True, "ready": True}
    assert "old" not in str(got).replace("old.trycloudflare", "") and "gsk_secret" not in str(got)


def test_a_choice_is_tried_then_saved_and_a_wrong_one_is_not(client, tried):
    c, path = client
    wrong = c.post("/assistant/model-settings", json={"use": "kaggle", "url": "https://new.trycloudflare.com/",
                                                      "password": "wrong"})
    assert wrong.status_code == 400 and "password is wrong" in wrong.json()["detail"]
    assert model_link.read(path)["KAGGLE_URL"] == "https://old.trycloudflare.com"  # nothing saved
    ok = c.post("/assistant/model-settings", json={"use": "kaggle", "url": "https://new.trycloudflare.com/",
                                                   "password": "right"})
    assert ok.json() == {"ok": True, "use": "kaggle", "model": "Qwen.gguf"}
    assert model_link.read(path) == {"KAGGLE_URL": "https://new.trycloudflare.com", "KAGGLE_PASSWORD": "right",
                                     "ZEN_MODEL": "kaggle"}


def test_the_notebooks_zen_code_or_its_printed_lines_paste_as_they_are(client, tried):
    c, path = client
    code = model_link.make_code("https://coded.trycloudflare.com", "fromcode")
    assert c.post("/assistant/model-settings", json={"use": "kaggle", "url": f"Zen code: {code}"}).status_code == 200
    assert model_link.read(path)["KAGGLE_PASSWORD"] == "fromcode"
    r = c.post("/assistant/model-settings", json={"use": "kaggle", "url": "  address : https://new.trycloudflare.com/",
                                                  "password": "password: right"})
    assert r.status_code == 200
    assert model_link.read(path)["KAGGLE_URL"] == "https://new.trycloudflare.com"
    assert model_link.read(path)["KAGGLE_PASSWORD"] == "right"


def test_a_switch_keeps_what_was_saved_and_a_choice_not_set_up_says_what_it_needs(client, tried):
    c, path = client
    cloud = c.post("/assistant/model-settings", json={"use": "cloud"})
    assert cloud.status_code == 400 and "the API's address" in cloud.json()["detail"]
    assert c.post("/assistant/model-settings", json={"use": "cloud", "url": "http://127.0.0.1:11434/v1",
                                                     "model": "llama3.2"}).json()["model"] == "llama3.2"
    assert c.post("/assistant/model-settings", json={"use": "local"}).json()["use"] == "local"
    back = c.post("/assistant/model-settings", json={"use": "cloud"})  # the green dot: nothing but the choice
    assert back.json() == {"ok": True, "use": "cloud", "model": "llama3.2"}
    assert tried[-1][1]["CLOUD_URL"] == "http://127.0.0.1:11434/v1"
    assert c.post("/assistant/model-settings", json={"use": "kaggle"}).status_code == 200  # the old password kept
    assert tried[-1][1]["KAGGLE_PASSWORD"] == "old"
    values = model_link.read(path)
    assert values["ZEN_MODEL"] == "kaggle" and values["LOCAL_URL"] == assistant_api.settings.llamacpp_url


def test_another_site_cannot_change_where_the_model_runs(client, tried):
    c, path = client
    r = c.post("/assistant/model-settings", json={"use": "cloud", "url": "https://evil.example/v1", "model": "x"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "CLOUD_URL" not in path.read_text(encoding="utf-8")
    bad = c.post("/assistant/model-settings", json={"use": "kaggle", "url": "ftp://x", "password": "x"})
    assert bad.status_code == 400 and "https://" in bad.json()["detail"]
    # a second line can't ride in with the key
    sneaky = c.post("/assistant/model-settings", json={"use": "cloud", "url": "https://api.x.com/v1", "model": "m",
                                                       "key": "k\nKAGGLE_URL=https://evil.example"})
    assert sneaky.status_code == 400 and "evil" not in path.read_text(encoding="utf-8")
    assert not tried  # none of them got as far as being tried


# ---------------------------------------------------------------- the app takes a change on its next request

def _settings(**kw):
    base = dict(llamacpp_url="http://127.0.0.1:8080", llamacpp_model="local", assistant_thinking=True,
                assistant_max_tokens=4096, assistant_thinking_budget=0, embed_gguf="")
    return SimpleNamespace(**{**base, **kw})


def _changed(path, changes):
    model_link.write(path, changes)
    os.utime(path, (time.time() + 5, time.time() + 5))  # a new time stamp, however fast the write was


def test_the_model_follows_the_settings_file_with_no_restart(tmp_path, monkeypatch):
    path = tmp_path / "model.env"
    monkeypatch.setattr(model_link, "search_at", lambda values, timeout=15: None)  # an older notebook: Qwen only
    monkeypatch.setattr(model_link, "check", lambda kind, values, timeout=20: (True, "x"))
    link = Connection(path, _settings())
    main, helpers = Live(link, 0), Live(link, 1)
    assert link.kind == "local" and main.name == "llamacpp" and main.base_url == "http://127.0.0.1:8080"
    assert (main.slot, helpers.slot) == (0, 1)
    _changed(path, {"ZEN_MODEL": "kaggle", "KAGGLE_URL": "https://one.trycloudflare.com", "KAGGLE_PASSWORD": "p"})
    assert main.base_url == "https://one.trycloudflare.com" and main.api_key == "p" and main.location() == "on Kaggle"
    _changed(path, {"ZEN_MODEL": "cloud", "CLOUD_URL": "https://api.groq.com/openai/v1", "CLOUD_KEY": "k",
                    "CLOUD_MODEL": "openai/gpt-oss-120b"})
    assert main.name == "cloud" and main.location() == "on groq.com" and helpers.model == "openai/gpt-oss-120b"
    assert isinstance(LiveEmbedder(link)._connection.embedder(), NoEmbedder)  # a cloud: search by keywords


def test_search_is_the_notebooks_model_kept_by_name_while_it_is_away(tmp_path, monkeypatch):
    path = tmp_path / "model.env"
    path.write_text("ZEN_MODEL=kaggle\n" + OLD, encoding="utf-8")
    found = {"at": ("https://old.trycloudflare.com/embed", "old", "embeddinggemma-2-Q8_0.gguf")}
    monkeypatch.setattr(model_link, "search_at", lambda values, timeout=15: found["at"])
    monkeypatch.setattr(model_link, "check", lambda kind, values, timeout=20: (False, "Nothing answers"))
    search = LiveEmbedder(Connection(path, _settings()))
    assert search.name == "embeddinggemma-2-Q8_0" and search.url == "https://old.trycloudflare.com/embed"
    assert search.api_key == "old"
    found["at"] = None  # the notebook stopped: the same model, by name, so nothing is vectorised again
    _changed(path, {"KAGGLE_URL": "https://new.trycloudflare.com"})
    assert search.name == connection.NOTEBOOK_SEARCH and search.url == "https://new.trycloudflare.com/embed"
    _changed(path, {"ZEN_MODEL": "local"})
    assert search.name == "none"  # a llama-server here, no search model: keywords


def test_this_computers_search_model_comes_first(tmp_path, monkeypatch):
    path = tmp_path / "model.env"
    path.write_text("ZEN_MODEL=kaggle\n" + OLD, encoding="utf-8")
    monkeypatch.setattr(model_link, "search_at", lambda values, timeout=15: pytest.fail("the notebook was asked"))
    search = LiveEmbedder(Connection(path, _settings(embed_gguf="C:/models/embeddinggemma-2-Q8_0.gguf", embed_server="",
                                                      embed_url="http://127.0.0.1:8070")))
    assert search.name == "embeddinggemma-2-Q8_0" and search.url == "http://127.0.0.1:8070"
    assert isinstance(search._connection.embedder(), ServerEmbedder)
