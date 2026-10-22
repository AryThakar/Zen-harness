# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The model's connection when it runs on Kaggle: the notebook's address and password, in the settings file the
bridge reads (kaggle/bridge.py; Zen-harness.exe's settings.env). A new notebook session prints a new address - changed
here, from the app, it is tested first and then written in place (every other line kept), and the bridge takes it on
its next request: no restart, the chat carries on.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

URL, PASSWORD = "KAGGLE_URL", "KAGGLE_PASSWORD"
USER_AGENT = "zen-harness/1.0 (+https://github.com/AryThakar/Zen-harness)"


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


def pasted(text: str, label: str) -> str:
    """What was pasted, less the label the notebook prints before it ("  address : https://...", "  password: ...")."""
    text = text.strip()
    if text[:len(label)].lower() == label:
        text = text[len(label):].lstrip(" :=")
    return text


def write(path: Path, url: str, password: str) -> None:
    """The address and password replaced where they are, or added; comments and other settings kept as they were."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = ["# The Kaggle notebook's last cell prints these two."]
    for name, value in ((URL, url), (PASSWORD, password)):
        at = next((i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == name), None)
        if at is None:
            lines.append(f"{name}={value}")
        else:
            lines[at] = f"{name}={value}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check(url: str, password: str, timeout: float = 20) -> tuple[bool, str]:
    """(ok, the model's name or why not): llama-server's /props, which needs the password - /health alone would pass a
    wrong one."""
    request = urllib.request.Request(url.rstrip("/") + "/props", headers={
        "Authorization": f"Bearer {password}", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            props = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "The password is wrong: copy it again from the notebook's last cell."
        if exc.code == 503:
            return False, "The notebook answers but its model is still loading: try again in a minute."
        return False, f"The address answered HTTP {exc.code}: is it the notebook's address?"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Nothing answers at that address ({getattr(exc, 'reason', exc)}): is the notebook running?"
    model = str(props.get("model_path") or props.get("model_alias") or "the model").replace("\\", "/").rsplit("/", 1)[-1]
    return True, model
