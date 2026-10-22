# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Where Zen's model runs - its Model settings, three choices, one in use:

    kaggle  the Kaggle notebook: its address and password - its last cell prints both, and a Zen code that is both in
            one line; a new session has a new address (the password stays when it is a Kaggle secret)
    local   a llama-server on this computer: its address
    cloud   any OpenAI-style API - a cloud's, or an app here such as Ollama: its address, a key and a model

They live in one settings file (ZEN_MODEL_SETTINGS: Zen-harness.exe's settings.env; from source model.env), changed
from the app: tried first, then written in place - every other line kept - and the app takes a change on its next
request (app/connection.py): no restart, the chat carries on. The notebook also runs Zen's search model, at /embed.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request
from pathlib import Path

from app.tools.net import USER_AGENT

USE = "ZEN_MODEL"
KAGGLE_URL, KAGGLE_PASSWORD = "KAGGLE_URL", "KAGGLE_PASSWORD"
LOCAL_URL = "LOCAL_URL"
CLOUD_URL, CLOUD_KEY, CLOUD_MODEL = "CLOUD_URL", "CLOUD_KEY", "CLOUD_MODEL"
KINDS = ("kaggle", "local", "cloud")
SECRETS = (KAGGLE_PASSWORD, CLOUD_KEY)  # never sent back to the page
LOCAL_DEFAULT = "http://127.0.0.1:8080"
EMBED = "/embed"  # the notebook's search model, under its address
CODE = "zen1."  # a Zen code: the notebook's address and password in one line, base64 (it copies whole)


def read(path: Path) -> dict[str, str]:
    """NAME=value lines of the settings file ({} when there is none)."""
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and not name.strip().startswith("#"):
            values[name.strip()] = value.strip()
    return values


def write(path: Path, changes: dict[str, str]) -> None:
    """Each setting replaced where it is, or added; comments and other settings kept as they were."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = ["# Zen's model settings - changed from the app (Settings, or the model's name in a chat)."]
    for name, value in changes.items():
        at = next((i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == name), None)
        if at is None:
            lines.append(f"{name}={value}")
        else:
            lines[at] = f"{name}={value}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def in_use(values: dict[str, str]) -> str:
    """The choice in use: the one saved, else Kaggle when the notebook's address is there, else this computer."""
    use = values.get(USE, "")
    if use in KINDS:
        return use
    return "kaggle" if values.get(KAGGLE_URL) else "local"


def ready(kind: str, values: dict[str, str]) -> bool:
    """Whether that choice has what it needs to be tried."""
    if kind == "kaggle":
        return bool(values.get(KAGGLE_URL) and values.get(KAGGLE_PASSWORD))
    if kind == "cloud":
        return bool(values.get(CLOUD_URL) and values.get(CLOUD_MODEL))
    return True  # this computer: its address, or the usual one


def local_url(values: dict[str, str], default: str = LOCAL_DEFAULT) -> str:
    return values.get(LOCAL_URL) or default


def pasted(text: str, label: str) -> str:
    """What was pasted, less the label the notebook prints before it ("  address : https://...", "  password: ...")."""
    text = text.strip()
    if text[:len(label)].lower() == label:
        text = text[len(label):].lstrip(" :=")
    return text


def code(text: str) -> tuple[str, str] | None:
    """The address and password in a Zen code the notebook printed, else None (not one)."""
    text = pasted(text, "zen code")
    if not text.startswith(CODE):
        return None
    data = text[len(CODE):]
    try:
        url, password = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8").split("\n", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return url.strip(), password.strip()


def make_code(url: str, password: str) -> str:
    return CODE + base64.urlsafe_b64encode(f"{url}\n{password}".encode()).decode("ascii").rstrip("=")


def _get(url: str, key: str = "", timeout: float = 20):
    headers = {"User-Agent": USER_AGENT, **({"Authorization": f"Bearer {key}"} if key else {})}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def check(kind: str, values: dict[str, str], timeout: float = 20) -> tuple[bool, str]:
    """(ok, the model's name or why not) for that choice: llama-server's /props (it needs the notebook's password -
    /health alone would pass a wrong one), a cloud's model list (it needs the key)."""
    if kind == "cloud":
        return _check_cloud(values, timeout)
    url = values.get(KAGGLE_URL, "") if kind == "kaggle" else local_url(values)
    key = values.get(KAGGLE_PASSWORD, "") if kind == "kaggle" else ""
    try:
        with _get(url.rstrip("/") + "/props", key, timeout) as response:
            props = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "The password is wrong: copy it again from the notebook's last cell."
        if exc.code == 503:
            return False, "It answers, but its model is still loading: try again in a minute."
        if exc.code == 404 and kind == "local":
            return False, ("That address answers, but not as llama-server. For an app such as Ollama or LM Studio, "
                           "choose Cloud and give its address.")
        return False, f"The address answered HTTP {exc.code}: is it the {'notebook' if kind == 'kaggle' else 'server'}'s address?"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        where = "is the notebook running, with this session's address?" if kind == "kaggle" else "is llama-server running?"
        return False, f"{_unanswered(exc, timeout)}: {where}"
    model = str(props.get("model_path") or props.get("model_alias") or "the model").replace("\\", "/").rsplit("/", 1)[-1]
    return True, model


def _check_cloud(values: dict[str, str], timeout: float) -> tuple[bool, str]:
    url, key, model = values.get(CLOUD_URL, "").rstrip("/"), values.get(CLOUD_KEY, ""), values.get(CLOUD_MODEL, "")
    try:
        with _get(url + "/models", key, timeout) as response:
            listed = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "The API key was refused: check it, and that it's for this address."
        if exc.code == 404:
            return False, "Nothing answers as an OpenAI-style API there: the address usually ends in /v1."
        return False, f"The address answered HTTP {exc.code}."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{_unanswered(exc, timeout)}: is the address right, and the internet on?"
    ids = [m.get("id", "") for m in listed.get("data", [])] if isinstance(listed, dict) else []
    if ids and model not in ids:
        near = [i for i in ids if model and model.lower() in i.lower()][:3] or ids[:3]
        return False, f"{model or 'No model'} isn't one this key can use; for example: {', '.join(near)}."
    return True, model


def _unanswered(exc: Exception, timeout: float) -> str:
    """Why nothing answered, in a few plain words (not Windows' own sentence)."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return "Nothing is running at that address"
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return f"No answer from that address in {timeout:.0f} seconds"
    if "getaddrinfo" in str(reason) or "Name or service" in str(reason):
        return "That address can't be found"
    return f"That address can't be reached ({str(reason)[:80]})"


def search_at(values: dict[str, str], timeout: float = 15) -> tuple[str, str, str] | None:
    """The notebook's search model - (its address, the password, its model file's name) - when Kaggle is in use and
    the notebook runs one (an older notebook has Qwen only); else None."""
    if in_use(values) != "kaggle" or not ready("kaggle", values):
        return None
    url = values[KAGGLE_URL].rstrip("/") + EMBED
    try:
        with _get(url + "/props", values[KAGGLE_PASSWORD], timeout) as response:
            props = json.loads(response.read() or b"{}")
        name = str(props["model_path"]).replace("\\", "/").rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - no search model there, however it fails
        return None
    return url, values[KAGGLE_PASSWORD], name
