"""run_python: run a short Python script for what no other tool covers.

The script runs in a separate Python process (the app's own interpreter, so the libraries in
INSTALLED are there: the project's "sandbox" extra) inside the workspace's sandbox folder, with:
- a time limit (default 30 s, at most 120 s); on timeout the whole process tree is killed
- a minimal environment: no API keys or other secrets from the app's environment; TEMP, TMP and
  HOME point into the sandbox; matplotlib draws off-screen and open figures are saved as images
- output cut to 10,000 characters (start and end kept)
- table("data_1") to read a workspace table (a copy - the original can't be changed) and path("file_1")
  for where a stored file is; open("file_1") and open("story.txt") (a stored file's original name)
  work too, because small models write that
Files the script writes are registered: .csv / .parquet become workspace tables, PNG / JPEG / WebP /
GIF become image items (with their size; one that isn't a readable image is kept as a file), anything
else a file. A script never changes a stored file (writing to a stored item's id is refused): stored
files change through files(update) or the coder's save.

The workspace is the boundary (the user's rule): an audit hook in the script refuses reading outside
the workspace and Python's own installation, listing other folders, writing, deleting or renaming
anywhere but the sandbox folder, starting programs or listing drives, and the network - DNS lookups
and connections, except a loopback connection to a temporary port (a socket pair inside the process,
e.g. an event loop on Windows); the model server and the app listen on low ports and stay out of
reach. Files come in only through files(download) with the user's approval, or an upload.
Deleting or renaming the workspace's stored items is refused too: deleting needs the user's approval.

This is NOT a security boundary: it is a separate process with limits, for a single user running
their own assistant locally. The hook sees what Python itself does - a native library that opens
files or sockets on its own (pyarrow's readers) is not covered.
Design points (clean environment, Agg backend, killing the tree) from Unsloth Studio's
sandbox (studio/backend/core/inference/tools.py), written independently and much smaller.
"""

from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

from .core import ToolError, ToolResult, jsonable
from .workspace import IMAGE_SUFFIXES, Workspace

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT = 10_000
MAX_CODE = 50_000
MAX_NEW_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
IMAGES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".svg": "svg", ".webp": "webp"}
CELL = "_cell.py"  # the set-up that runs the script
SCRIPT = "script.py"  # the model's code

PRELUDE = '''# --- added by run_python ---
import json as _json, os as _os, sys as _sys
_WS = {ws!r}

# Files on this computer outside the workspace are off limits (the user's rule): an audit hook
# refuses reading them, listing their folders, writing anywhere but here, starting programs - and the
# network: data enters and leaves the workspace only through cards the user clicks (files' download).
def _norm(p):
    return _os.path.normcase(_os.path.realpath(p))
_HERE = _norm(_os.getcwd())
_READ = [_norm(p) for p in {{_WS, _os.getcwd(), _sys.prefix, _sys.base_prefix, _sys.exec_prefix,
                             _os.path.join(_os.environ.get("SystemRoot", r"C:\\Windows"), "Fonts")}}]
_WRITE_FLAGS = _os.O_WRONLY | _os.O_RDWR | _os.O_APPEND | _os.O_CREAT | _os.O_TRUNC
_CHANGE = {{"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.chmod", "os.truncate", "os.utime", "os.link",
           "os.symlink", "shutil.rmtree"}}
_PROGRAMS = {{"subprocess.Popen", "os.system", "os.startfile", "os.exec", "os.spawn", "os.posix_spawn",
             "_winapi.CreateProcess", "os.listdrives", "os.listvolumes", "os.listmounts"}}
_NET = {{"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.sendto",
        "socket.sendmsg"}}
_LOOPBACK = {{"127.0.0.1", "::1", "localhost"}}
_checking = False

def _local_pair(event, args):
    # A socket pair inside this process (Windows builds one through a loopback connect on a temporary
    # port, e.g. for an event loop) - not the network. Services on this computer (the model, the app)
    # listen on low ports and stay out of reach.
    if event == "socket.getaddrinfo":
        return str(args[0]) in _LOOPBACK
    address = args[1] if len(args) > 1 else None
    return (isinstance(address, tuple) and len(address) >= 2 and str(address[0]) in _LOOPBACK
            and isinstance(address[1], int) and address[1] >= 49152)

def _inside(p, roots):
    if isinstance(p, int) or p is None:
        return True  # an open file descriptor, or the current folder
    try:
        p = _norm(_os.fsdecode(_os.fspath(p)))
    except (TypeError, ValueError, OSError):
        return True  # not a path
    return any(p == r or p.startswith(r.rstrip(_os.sep) + _os.sep) for r in roots)

_STORE = _norm(_WS)

def _refuse(what, path=None):
    # Also said on stderr: os.walk and glob swallow the error and would just find nothing.
    if path is not None and _inside(path, [_STORE]):  # the workspace's own store, not the computer's files
        _sys.stderr.write(f"[not allowed] {{what}}\\n")
        raise PermissionError(f"{{what}}: a script never changes stored items - write a new file instead. Stored "
                              "files change through files(update) or the coder's save; deleting needs the "
                              "user's approval.")
    _sys.stderr.write(f"[refused] {{what}}\\n")
    raise PermissionError(f"{{what}}: files on the user's computer are off limits. Use the workspace "
                          "(table('data_1'), path('file_1')) and files this script writes here.")

def _guard(event, args):
    global _checking
    if _checking or not (event == "open" or event in _CHANGE or event in _PROGRAMS or event in _NET
                         or event in ("os.listdir", "os.scandir")):
        return
    _checking = True
    try:
        if event in _PROGRAMS:
            _refuse("Starting programs is not allowed")
        if event in _NET and not _local_pair(event, args):
            _sys.stderr.write("[refused] the internet\\n")
            raise PermissionError("Scripts have no internet access: files come in through files(mode='download') "
                                  "with the user's approval, or the user uploads them.")
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None))[:3]
            writes = bool(mode and any(c in str(mode) for c in "wax+")) or bool((flags or 0) & _WRITE_FLAGS)
            if not _inside(path, [_HERE] if writes else _READ):
                _refuse(f"{{'Writing' if writes else 'Reading'}} {{path}}", path)
        elif event in ("os.listdir", "os.scandir"):
            if args and not _inside(args[0], _READ):
                _refuse(f"Listing {{args[0]}}")
        else:
            for a in args:
                if isinstance(a, (str, bytes, _os.PathLike)) and not _inside(a, [_HERE]):
                    _refuse(f"Changing {{a}}", a)
    finally:
        _checking = False
_sys.addaudithook(_guard)

import builtins as _builtins
_open = _builtins.open
_registry = None

def _items():
    """The workspace's items, read once (a script can't change them); none yet in a new workspace."""
    global _registry
    if _registry is None:
        try:
            with _open(_os.path.join(_WS, "registry.json"), encoding="utf-8") as f:
                _registry = _json.load(f)["items"]
        except FileNotFoundError:
            _registry = {{}}
    return _registry

def table(item_id):
    """A copy of a workspace table as a pandas DataFrame (e.g. table("data_1"))."""
    import pandas as _pd
    items = _items()
    if items.get(item_id, {{}}).get("kind") != "table":
        raise KeyError(f"No table {{item_id!r}}. Tables: {{', '.join(i for i, e in items.items() if e['kind'] == 'table')}}")
    return _pd.read_parquet(_os.path.join(_WS, item_id + ".parquet"))

def path(item_id):
    """Where a stored file, note or image is (e.g. open(path("file_1"))). Read it; write new files here instead."""
    items = _items()
    if "file" not in items.get(item_id, {{}}):
        raise KeyError(f"No stored file {{item_id!r}}. Files: {{', '.join(i for i, e in items.items() if 'file' in e)}}")
    return _os.path.join(_WS, items[item_id]["file"])

def _resolve(name):
    """'file_1' -> the stored file (always); a stored file's original name -> its path when no such
    file is here."""
    if not isinstance(name, str):
        return name
    items = _items()
    if "file" in items.get(name, {{}}):
        return _os.path.join(_WS, items[name]["file"])
    if _os.path.exists(name):
        return name
    for e in items.values():
        if e.get("name") == _os.path.basename(name) and "file" in e:
            return _os.path.join(_WS, e["file"])
    return name

def _open_resolving(file, mode="r", *args, **kwargs):
    if not any(c in mode for c in "wax+"):
        return _open(_resolve(file), mode, *args, **kwargs)
    if isinstance(file, str) and file in _items():
        # A script makes new files; it never changes a stored one (that is the files tool's update).
        raise PermissionError(f"{{file}} is a stored item's id: write the result under a new file name.")
    return _open(file, mode, *args, **kwargs)
_builtins.open = _open_resolving

def _images():
    return {{n: _os.stat(n).st_mtime for n in _os.listdir(".") if n.lower().endswith({images!r})}}
_images_before = _images()

def _save_figures():
    # Figures still open at the end (plt.show() draws nothing here) are saved as images - unless the
    # script saved images itself. Run right after the script: matplotlib's own exit handler closes them.
    plt = _sys.modules.get("matplotlib.pyplot")
    if plt is None or any(_images_before.get(n) != t for n, t in _images().items()):
        return
    for n in plt.get_fignums():
        plt.figure(n).savefig(f"figure_{{n}}.png", dpi=110, bbox_inches="tight")

import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*non-interactive, and thus cannot be shown")
# The script runs as its own file, so its errors give its own line numbers.
try:
    with _open({script!r}, encoding="utf-8") as _f:
        _code = compile(_f.read(), {script!r}, "exec")
    exec(_code, globals())
finally:
    _save_figures()
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
    (box / CELL).write_text(PRELUDE.format(ws=str(ws.root.resolve()), script=SCRIPT, images=tuple(IMAGES)),
                            encoding="utf-8")
    (box / SCRIPT).write_text(code, encoding="utf-8")
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
    errors = OWN_FRAME.sub("", _cut(err.decode("utf-8", "replace")).replace(str(box), "."))
    if errors.strip():
        result["errors"] = errors
    if files:
        result["files"] = files
    if "[refused] " in errors:
        result["note"] = ("The script was refused access to files on the user's computer: any count or list it "
                          "printed about them is not real. Tell the user this can't be done here.")
    if timed_out:
        result["error"] = {"message": f"Stopped after {timeout} s (time limit). Make the script faster or smaller."}
        status = "failed"
    elif proc.returncode != 0:
        result["error"] = {"message": "The script raised an error (see errors); fix the code and run it again."
                                      + _hint(errors)}
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
           "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(tmp), "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8",
           # n_jobs=-1 (scikit-learn, joblib) runs in this process instead of starting worker programs.
           "JOBLIB_MULTIPROCESSING": "0"}
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


# A traceback frame of the set-up code (with its source and marker lines): not the model's code.
OWN_FRAME = re.compile(rf'^  File "[^"\n]*{re.escape(CELL)}", line \d+, in [^\n]*\n(?:    [^\n]*\n)*', re.M)
MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '(\w+)")
# What a script can import, by import name (the project's "sandbox" extra installs most of them).
# What scripts are told they can import. requests and bs4 are installed but not offered: scripts have no
# internet (the audit hook), so they would only fail.
LIBRARIES = ("numpy", "pandas", "scipy", "sklearn", "statsmodels", "matplotlib", "seaborn", "sympy", "networkx",
             "polars", "xgboost", "lightgbm", "imblearn", "cv2", "skimage", "PIL", "nltk",
             "openpyxl", "xlrd", "xlsxwriter", "docx", "pptx", "pypdf", "duckdb", "pyarrow", "tabulate")
INSTALLED = tuple(name for name in LIBRARIES if importlib.util.find_spec(name))
ID_AS_VARIABLE = re.compile(r"NameError: name '((?:data|file|model|image|note)_\d+\w*)' is not defined")
ID_AS_SQL_TABLE = re.compile(r"Table with name ((?:data)_\d+\w*) does not exist")
ID_AS_PATH = re.compile(r"(?:FileNotFoundError|No such file).*?'((?:data|file|model|image|note)_\d+\w*)'")


def _hint(errors: str) -> str:
    """The fix for mistakes small models repeat run after run: a workspace id used as a variable,
    or given to a library that opens files itself (pandas does not go through open()); and what to
    do when the script reached for the computer's files."""
    if "files on the user's computer are off limits" in errors:
        return (" Scripts can't read or change files on the user's computer, start programs or list drives - "
                "only workspace items and files written here. Tell the user that instead of trying again.")
    m = MISSING_MODULE.search(errors)
    if m and m[1] not in INSTALLED:  # measured: the coder retried the same import four times
        return (f" {m[1]} is not installed here and nothing can be installed: don't import it again. Use the "
                f"standard library or {', '.join(INSTALLED)}.")
    m = ID_AS_SQL_TABLE.search(errors)
    if m:  # duckdb inside a script doesn't see the workspace (query_data does)
        return (f" In a script, read a table with df = table('{m[1]}') and query the DataFrame "
                f"(duckdb.sql('SELECT ... FROM df')).")
    m = ID_AS_VARIABLE.search(errors)
    if m:
        return (f" Workspace items are not Python variables: use df = table('{m[1]}') for a table, path('{m[1]}') "
                "for a file. To keep a new table, write it to a file (df.to_parquet('name.parquet')); it becomes "
                "a workspace table.")
    m = ID_AS_PATH.search(errors)
    if m:
        return (f" '{m[1]}' is an id, not a file name: use path('{m[1]}') for its location (e.g. "
                f"pd.read_csv(path('{m[1]}'))), or table('{m[1]}') if it is a table.")
    return ""


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n... ({len(text) - MAX_OUTPUT:,} characters cut) ...\n{text[-half:]}"


def _snapshot(box: Path) -> dict[Path, tuple[int, float]]:
    return {p: (p.stat().st_size, p.stat().st_mtime) for p in box.rglob("*")
            if p.is_file() and ".tmp" not in p.relative_to(box).parts}


def _register(ws: Workspace, box: Path, before: dict) -> list[dict]:
    """Workspace ids for the files the script created or changed - new items; a script never changes
    a stored one."""
    after = _snapshot(box)
    new = [p for p, sig in after.items() if p.name not in (CELL, SCRIPT) and before.get(p) != sig]
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
                ws.set_meta(item, "from", "run_python")  # made by the assistant, like its images and files
            elif suffix == ".parquet":
                item = ws.add_table(pd.read_parquet(p), name=rel, meta={"from": "run_python"})
            elif suffix in IMAGE_SUFFIXES:  # SVG stays a file: it can carry scripts, the UI never shows it inline
                try:
                    item = ws.add_image(p.read_bytes(), rel, meta={"from": "run_python"})
                except ToolError:
                    item = ws.add_bytes("file", p.read_bytes(), suffix.lstrip("."), name=rel, meta={"from": "run_python"})
                    out.append({"path": rel, "id": item, "note": "not a readable image; kept as a file"})
                    continue
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
