"""run_python: run a short Python script for what no other tool covers.

The script runs in a separate Python process (the app's own interpreter, so pandas, numpy, scipy
and scikit-learn are there) inside the workspace's sandbox folder, with:
- a time limit (default 30 s, at most 120 s); on timeout the whole process tree is killed
- a minimal environment: no API keys or other secrets from the app's environment; TEMP, TMP and
  HOME point into the sandbox; matplotlib draws off-screen and open figures are saved as images
- output cut to 10,000 characters (start and end kept)
- table("data_1") to read a workspace table (a copy - the original can't be changed)
Files the script writes are registered: .csv / .parquet become workspace tables, images become
images, anything else a file.

This is NOT a security boundary: it is a separate process with limits, for a single user running
their own assistant locally. It does not stop code from reading files or using the network.
Design points (clean environment, Agg backend, killing the tree) from Unsloth Studio's
sandbox (studio/backend/core/inference/tools.py), written independently and much smaller.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

from .core import ToolError, ToolResult, jsonable
from .workspace import Workspace

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT = 10_000
MAX_CODE = 50_000
MAX_NEW_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
IMAGES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".svg": "svg", ".webp": "webp"}
CELL = "_cell.py"

PRELUDE = '''# --- added by run_python ---
import atexit as _atexit, json as _json, os as _os
_WS = {ws!r}

def table(item_id):
    """A copy of a workspace table as a pandas DataFrame (e.g. table("data_1"))."""
    import pandas as _pd
    with open(_os.path.join(_WS, "registry.json"), encoding="utf-8") as f:
        items = _json.load(f)["items"]
    if items.get(item_id, {{}}).get("kind") != "table":
        raise KeyError(f"No table {{item_id!r}}. Tables: {{', '.join(i for i, e in items.items() if e['kind'] == 'table')}}")
    return _pd.read_parquet(_os.path.join(_WS, item_id + ".parquet"))

def _save_figures():
    import sys as _sys
    if "matplotlib.pyplot" in _sys.modules:
        plt = _sys.modules["matplotlib.pyplot"]
        for n in plt.get_fignums():
            plt.figure(n).savefig(f"figure_{{n}}.png", dpi=110, bbox_inches="tight")
_atexit.register(_save_figures)
# --- your code ---
'''


def run_python(ws: Workspace, code: str, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    """Run a Python script; returns its printed output, errors and the files it made."""
    code = str(code)
    if not code.strip():
        return _invalid("code is empty.", "code")
    if len(code) > MAX_CODE:
        return _invalid(f"code is longer than {MAX_CODE:,} characters.", "code")
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    box = ws.root / "sandbox"
    tmp = box / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (box / CELL).write_text(PRELUDE.format(ws=str(ws.root.resolve())) + code, encoding="utf-8")
    before = _snapshot(box)

    started = time.perf_counter()
    proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8", CELL], cwd=box, env=_env(box, tmp),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            **_new_group())
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    seconds = round(time.perf_counter() - started, 2)

    files = _register(ws, box, before)
    result = {"exit_code": proc.returncode, "seconds": seconds,
              "output": _cut(out.decode("utf-8", "replace"))}
    errors = _cut(err.decode("utf-8", "replace")).replace(str(box), ".")
    if errors.strip():
        result["errors"] = errors
    if files:
        result["files"] = files
    if timed_out:
        result["error"] = {"message": f"Stopped after {timeout} s (time limit). Make the script faster or smaller."}
        status = "failed"
    elif proc.returncode != 0:
        result["error"] = {"message": "The script raised an error (see errors); fix the code and run it again."}
        status = "invalid_arguments"
    else:
        status = "ok"
    artifact = {"type": "code", "code": code, "output": result["output"], "errors": result.get("errors", ""),
                "files": files}
    return ToolResult(status, jsonable(result), jsonable(artifact))


def _invalid(message: str, field: str) -> ToolResult:
    return ToolResult("invalid_arguments", {"error": {"message": message, "field": field}})


def _env(box: Path, tmp: Path) -> dict:
    """Only what Python needs - nothing from the app's environment (API keys, tokens) leaks in."""
    env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("SystemRoot", r"C:\Windows")]),
           "HOME": str(box), "USERPROFILE": str(box), "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
           "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(tmp), "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}
    if sys.platform == "win32":
        env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    return env


def _new_group() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the script and anything it started."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n... ({len(text) - MAX_OUTPUT:,} characters cut) ...\n{text[-half:]}"


def _snapshot(box: Path) -> dict[Path, tuple[int, float]]:
    return {p: (p.stat().st_size, p.stat().st_mtime) for p in box.rglob("*")
            if p.is_file() and ".tmp" not in p.relative_to(box).parts}


def _register(ws: Workspace, box: Path, before: dict) -> list[dict]:
    """Workspace ids for the files the script created or changed."""
    after = _snapshot(box)
    new = [p for p, sig in after.items() if p.name != CELL and before.get(p) != sig]
    out = []
    for p in sorted(new)[:MAX_NEW_FILES]:
        rel = p.relative_to(box).as_posix()
        if p.stat().st_size > MAX_FILE_BYTES:
            out.append({"path": rel, "note": "larger than 20 MB; not registered"})
            continue
        suffix = p.suffix.lower()
        try:
            if suffix == ".csv":
                item = ws.add_file(p)
            elif suffix == ".parquet":
                item = ws.add_table(pd.read_parquet(p), name=rel)
            elif suffix in IMAGES:
                item = ws.add_bytes("image", p.read_bytes(), IMAGES[suffix], name=rel, meta={"from": "run_python"})
            else:
                item = ws.add_bytes("file", p.read_bytes(), suffix.lstrip(".") or "bin", name=rel,
                                    meta={"from": "run_python"})
        except (ToolError, ValueError, OSError) as exc:
            out.append({"path": rel, "note": f"not registered: {exc}"})
            continue
        out.append({"path": rel, "id": item})
    if len(new) > MAX_NEW_FILES:
        out.append({"note": f"{len(new) - MAX_NEW_FILES} more files were written but not registered"})
    return out
