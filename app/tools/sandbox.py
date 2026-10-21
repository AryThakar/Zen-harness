# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""run_python: run a short Python script for what no other tool covers.

The script runs in a separate Python process (the app's own interpreter, so the libraries in
INSTALLED are there: the project's "sandbox" extra) **in the workspace's own folder**, so it opens the
workspace's files by their paths - pd.read_csv('sales.csv'), Image.open('panda.jpg') - with:
- a time limit (default 30 s, at most 120 s); on timeout the whole process tree is killed
- a minimal environment: no API keys or other secrets from the app's environment; TEMP, TMP and
  HOME point into the workspace's .zen/run; matplotlib draws off-screen and open figures are saved as images
- output cut to 10,000 characters (start and end kept)
- typed input (stdin), when given: what a person would type, one line per input() call - so a program
  that asks (a game, a menu) can be run and tested; without it, input() ends the run with EOFError
- table('sales.csv') for a table's typed copy (dates and numbers already read), path('x') for where a file is
Files the script writes are registered through the workspace's one write path (Workspace.adopt): a new
file becomes a workspace file (.csv / .parquet a table, PNG / JPEG / WebP / GIF an image, anything else a
file); a file the assistant made that the script writes again is that file's new content, its version
before kept.

The workspace is the boundary (the user's rule): an audit hook in the script refuses reading outside
the workspace and Python's own installation, listing other folders, writing anywhere but the workspace's
files, changing a file the user brought (that is files edit or write, which asks them), deleting or moving a
workspace file (files(delete) asks; files(rename) moves), starting programs or listing drives, and the
network - DNS lookups and connections, except a loopback connection to a temporary port (a socket pair
inside the process, e.g. an event loop on Windows); the model server and the app listen on low ports and
stay out of reach. Files come in only through files(download) with the user's approval, or an upload.

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
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

from .core import ToolError, ToolResult, jsonable
from .filetypes import IMAGE
from .chart_image import describe_chart, is_chart
from .workspace import Workspace, kind_of, prune_versions

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT = 10_000
MAX_CODE = 50_000
MAX_NEW_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024
IMAGES = (*IMAGE, ".svg")  # what counts as a figure the script saved itself (then open figures aren't saved again)
CELL = "_cell.py"  # the set-up that runs the script
SCRIPT = "script.py"  # the model's code (its tracebacks name this file)
# matplotlib's font cache, one for every project: in each run's own folder it was built again for each new project,
# 1-4 s - and over 5 s on a busy machine, when matplotlib says so on stderr and the run read as failed (a test that
# failed only in full runs, 2026-10-21)
MPL_CACHE = Path(tempfile.gettempdir()) / "zen-harness-matplotlib"
MPL_NOTICE = re.compile(r"^Matplotlib is building the font cache; this may take a moment\.\r?\n?", re.M)

PRELUDE = '''# --- added by run_python ---
import json as _json, os as _os, sys as _sys, shutil as _shutil, datetime as _dt
_WS = {ws!r}
_ZEN = _os.path.join(_WS, ".zen")

# Files on this computer outside the workspace are off limits (the user's rule): an audit hook
# refuses reading them, listing their folders, writing anywhere but the workspace's files, changing the
# user's own files, deleting or moving workspace files, starting programs - and the network: data enters
# and leaves the workspace only through cards the user clicks (files' download).
def _norm(p):
    return _os.path.normcase(_os.path.realpath(p))
_HERE = _norm(_os.getcwd())
# the files, the run's own temporary folder, and matplotlib's font cache (one for every project: MPL_CACHE)
_WRITABLE = [_HERE, _norm(_os.environ.get("TEMP") or _os.getcwd()), _norm({mpl!r})]
_READ = [_norm(p) for p in {{_WS, _os.getcwd(), _sys.prefix, _sys.base_prefix, _sys.exec_prefix, {mpl!r},
                             _os.path.join(_os.environ.get("SystemRoot", r"C:\\Windows"), "Fonts")}}]
_TRACKED = {{_norm(_os.path.join(_WS, f)): k for k, f in {tracked!r}.items()}}
_PROTECTED = {{_norm(_os.path.join(_WS, f)) for f in {protected!r}}}
_kept = set()
_WRITE_FLAGS = _os.O_WRONLY | _os.O_RDWR | _os.O_APPEND | _os.O_CREAT | _os.O_TRUNC
_CHANGE = {{"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.chmod", "os.truncate", "os.utime", "os.link",
           "os.symlink", "shutil.rmtree"}}
_GONE = {{"os.remove", "os.rmdir", "os.link", "os.symlink", "shutil.rmtree"}}
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

def _path(p):
    if isinstance(p, int) or p is None:
        return None  # an open file descriptor, or the current folder
    try:
        return _norm(_os.fsdecode(_os.fspath(p)))
    except (TypeError, ValueError, OSError):
        return None  # not a path

def _inside(p, roots):
    p = _path(p)
    return p is None or any(p == r or p.startswith(r.rstrip(_os.sep) + _os.sep) for r in roots)

def _refuse(what):
    # Also said on stderr: os.walk and glob swallow the error and would just find nothing.
    _sys.stderr.write(f"[refused] {{what}}\\n")
    raise PermissionError(f"{{what}}: files on the user's computer are off limits. Use the workspace's files by "
                          "their names (pd.read_csv('sales.csv'), open('notes.md')) and files this script writes.")

def _not_here(p, why):
    _sys.stderr.write(f"[not allowed] {{why}}\\n")
    raise PermissionError(why)

def _keep(p):
    # A file the assistant made, about to change: its version before is kept (once per run).
    if p in _kept or not _os.path.exists(p):
        return
    _kept.add(p)
    folder = _os.path.join(_ZEN, "versions", _TRACKED[p])
    _os.makedirs(folder, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    _shutil.copy2(p, _os.path.join(folder, stamp + _os.path.splitext(p)[1]))

def _own(p):
    return _not_here(p, f"{{_os.path.basename(p)}} is the user's own file: a script never changes it - write the "
                        "result to a new file. To change the user's file, use files with mode='edit' or mode='write': the user is asked.")

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
            if not _inside(path, _WRITABLE if writes else _READ):
                _refuse(f"{{'Writing' if writes else 'Reading'}} {{path}}")
            p = _path(path)
            if writes and p in _PROTECTED:
                _own(p)
            if writes and p in _TRACKED:
                _keep(p)
        elif event in ("os.listdir", "os.scandir"):
            if args and not _inside(args[0], _READ):
                _refuse(f"Listing {{args[0]}}")
        else:
            paths = [a for a in args if isinstance(a, (str, bytes, _os.PathLike))]
            for a in paths:
                if not _inside(a, _WRITABLE):
                    _refuse(f"Changing {{a}}")
            norm = [_path(a) for a in paths]
            if any(p in _PROTECTED for p in norm):
                _own(next(p for p in norm if p in _PROTECTED))
            if event in _GONE or (event == "os.rename" and norm and norm[0] in _TRACKED):
                if any(p in _TRACKED for p in norm):
                    _not_here(norm[0], "Deleting or moving a workspace file is not done by a script: files(delete) "
                                       "asks the user, files(rename) moves one.")
            for p in norm:
                if p in _TRACKED:
                    _keep(p)
    finally:
        _checking = False
_sys.addaudithook(_guard)

import builtins as _builtins
_open = _builtins.open
_registry = None

def _items():
    """The workspace's files, read once (a script can't change the registry); none yet in a new workspace."""
    global _registry
    if _registry is None:
        try:
            with _open(_os.path.join(_ZEN, "registry.json"), encoding="utf-8") as f:
                _registry = _json.load(f)["items"]
        except FileNotFoundError:
            _registry = {{}}
    return _registry

def _key(name):
    items = _items()
    text = str(name).replace("\\\\", "/").strip().strip("/")
    if text in items:
        return text
    low = text.casefold()
    for k, e in items.items():
        if e["name"].casefold() == low:
            return k
    base = [k for k, e in items.items() if e["name"].rsplit("/", 1)[-1].casefold() == low]
    return base[0] if len(base) == 1 else None

def table(name):
    """A workspace table as a pandas DataFrame, its dates and numbers already read (e.g. table('sales.csv'))."""
    import pandas as _pd
    items, k = _items(), _key(name)
    if k is None or items[k]["kind"] != "table":
        raise KeyError(f"No table {{name!r}}. Tables: {{', '.join(e['name'] for e in items.values() if e['kind'] == 'table')}}")
    cache = _os.path.join(_ZEN, "cache", k + ".parquet")
    if _os.path.exists(cache):
        return _pd.read_parquet(cache)
    return _pd.read_csv(_os.path.join(_WS, items[k]["file"]))

def path(name):
    """Where a workspace file is (e.g. open(path('notes.md'))) - its name works as it is, too."""
    items, k = _items(), _key(name)
    if k is None or items[k]["kind"] == "note":
        raise KeyError(f"No file {{name!r}}. Files: {{', '.join(e['name'] for e in items.values())}}")
    return _os.path.join(_WS, items[k]["file"])

def _open_resolving(file, mode="r", *args, **kwargs):
    # A first-design key ('file_1') in a script saved back then: opened as the file it names.
    if isinstance(file, str) and file in _items() and not _os.path.exists(file):
        file = path(file)
    return _open(file, mode, *args, **kwargs)
_builtins.open = _open_resolving

_CHARTS = ("line", "area", "bar", "scatter", "histogram", "pie")

def chart(type, x, y=None, series=None, title=None, x_label=None, y_label=None):
    """A chart the page draws live, like make_chart's: chart('line', x=[...], y=[...], title='...'), or several
    lines or bars with series=[{{'name': 'A', 'y': [...]}}, ...]. Written as charts/<title>.chart.json; returns its
    name. A picture that isn't a chart (an image, a map, 3D) is drawn with matplotlib instead."""
    import re as _re
    if type not in _CHARTS:
        raise ValueError(f"chart type must be one of {{', '.join(_CHARTS)}}")
    tolist = lambda v: v.tolist() if hasattr(v, "tolist") else list(v)
    spec = {{"chart": type, "title": str(title or y_label or "chart"), "x": tolist(x),
            "x_label": x_label, "y_label": y_label}}
    if series is not None:
        spec["series"] = [{{"name": str(s["name"]), "y": tolist(s["y"])}} for s in series]
    elif y is not None:
        spec["y"] = tolist(y)
    else:
        raise ValueError("chart needs y (the values) or series")
    for s in spec.get("series") or [{{"y": spec.get("y")}}]:
        if len(s["y"]) != len(spec["x"]):
            raise ValueError(f"x has {{len(spec['x'])}} values but y has {{len(s['y'])}}")
    slug = "-".join(_re.findall(r"[a-z0-9]+", spec["title"].lower()))[:40].strip("-") or "chart"
    _os.makedirs("charts", exist_ok=True)
    name = f"charts/{{slug}}.chart.json"
    with _open(name, "w", encoding="utf-8") as f:
        _json.dump(spec, f, default=str)
    return name

# table, path and chart are a script's own already; importing them works too (measured: 'from chart import chart'
# stopped a script with "chart is not installed"). zen holds all three.
import types as _types
_zen = _types.ModuleType("zen", "The workspace's helpers: table(name), path(name), chart(type, x, y, ...).")
_zen.table, _zen.path, _zen.chart = table, path, chart
_sys.modules["zen"] = _zen
for _n, _f in (("chart", chart), ("table", table)):
    _m = _types.ModuleType(_n)
    setattr(_m, _n, _f)
    _sys.modules.setdefault(_n, _m)

def _images():
    # every folder but hidden ones: a script that saved its pictures under work/ saved them itself (measured: its
    # open figures were saved again as figure_1.png beside them)
    found = {{}}
    for _root, _dirs, _files in _os.walk("."):
        _dirs[:] = [d for d in _dirs if not d.startswith(".")]
        for n in _files:
            if n.lower().endswith({images!r}):
                found[_os.path.join(_root, n)] = _os.stat(_os.path.join(_root, n)).st_mtime
    return found
_images_before = _images()

def _save_figures():
    # Figures still open at the end (plt.show() draws nothing here) are saved as images - unless the
    # script saved images itself - in the script's own folder. Run right after the script: matplotlib's
    # own exit handler closes them.
    plt = _sys.modules.get("matplotlib.pyplot")
    if plt is None or any(_images_before.get(n) != t for n, t in _images().items()):
        return
    folder = "" if _os.path.isabs({name!r}) else _os.path.dirname({name!r})
    for n in plt.get_fignums():
        plt.figure(n).savefig(_os.path.join(folder, f"figure_{{n}}.png"), dpi=110, bbox_inches="tight")

import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*non-interactive, and thus cannot be shown")
try:
    import pandas as _pd_display  # printed tables in full: a cut "..." column hid the numbers a helper needed
    _pd_display.set_option("display.width", 250)
    _pd_display.set_option("display.max_columns", 50)
    _pd_display.set_option("display.max_colwidth", 80)
except ImportError:
    pass
# The script runs as its own file, so its errors give its own line numbers. Open figures are saved only from a run
# that finished: a failed run's figure is half drawn (measured: an empty set of axes shown as work/figure_1.png).
try:
    with _open({script!r}, encoding="utf-8") as _f:
        _code = compile(_f.read(), {name!r}, "exec")
    exec(_code, globals())
except SystemExit as _exit:
    if _exit.code in (None, 0):
        _save_figures()
    raise
else:
    _save_figures()
'''


class Engine(Protocol):
    """Where a helper's script runs: one call, the same result for every engine (printed output, errors, the
    files it made as workspace files, a code card). Today's is LocalPython; a real sandbox (a container, a VM)
    would be another class with this run(), and nothing else in the app would change."""
    name: str

    def run(self, ws: Workspace, code: str, timeout: int = DEFAULT_TIMEOUT, stdin: str | None = None,
            name: str | None = None) -> ToolResult: ...


class LocalPython:
    """A separate Python process on this computer, with the limits and the audit hook above."""
    name = "local Python"

    def run(self, ws: Workspace, code: str, timeout: int = DEFAULT_TIMEOUT, stdin: str | None = None,
            name: str | None = None) -> ToolResult:
        return run_python(ws, code, timeout, stdin, name)


ENGINE: Engine = LocalPython()  # run.py runs every program through it


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(folder: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(folder.resolve()), threading.Lock())


def run_python(ws: Workspace, code: str, timeout: int = DEFAULT_TIMEOUT, stdin: str | None = None,
               name: str | None = None) -> ToolResult:
    """Run a Python script in the workspace's folder; returns its printed output, errors and the files it wrote
    or changed (by path; a file written again in a later run is the same file, updated). ``name``: the workspace
    file the code is (its tracebacks then name it, with its lines)."""
    code = str(code)
    if not code.strip():
        return _invalid("code is empty.", "code")
    if len(code) > MAX_CODE:
        return _invalid(f"code is longer than {MAX_CODE:,} characters.", "code")
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    run_dir = ws.zen / "run"
    with _lock(ws.root):  # one run at a time in a workspace: each writes the script and registers what changed
        argv = _prepare(ws, code, name, run_dir)
        before = _snapshot(ws.files)

        started = time.perf_counter()
        typed = stdin is not None and stdin != ""
        proc = subprocess.Popen(argv, cwd=ws.files, env=_env(run_dir, run_dir / "tmp"), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.PIPE if typed else subprocess.DEVNULL,
                                **_new_group())
        timed_out = False
        try:
            out, err = proc.communicate(input=(stdin if stdin.endswith("\n") else stdin + "\n").encode("utf-8")
                                        if typed else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            out, err = proc.communicate()
        seconds = round(time.perf_counter() - started, 2)

        files, made = _register(ws, before)
    result = {"exit_code": proc.returncode, "seconds": seconds,
              "output": _cut(out.decode("utf-8", "replace"))}
    errors = own_errors(_cut(err.decode("utf-8", "replace")), ws, run_dir)
    if errors.strip():
        result["errors"] = errors
    if files:
        result["files"] = files
    if "[refused] the internet" in errors:  # also when the script caught the error and "ran"
        result["note"] = NO_INTERNET
    elif "[refused] " in errors:
        result["note"] = ("The script was refused access to files on the user's computer: any count or list it "
                          "printed about them is not real. Tell the user this can't be done here.")
    if timed_out:
        result["error"] = {"message": f"Stopped after {timeout} s (time limit). Make the script faster or smaller."}
        status = "failed"
    elif proc.returncode != 0:
        result["error"] = {"message": "The script raised an error (see errors); fix the code and run it again."
                                      + _hint(errors, ws)}
        status = "invalid_arguments"
    else:
        status = "ok"
    artifact = {"type": "code", "code": code, "output": result["output"], "errors": result.get("errors", ""),
                "files": made}
    return ToolResult(status, jsonable(result), jsonable(artifact))


def _prepare(ws: Workspace, code: str, name: str | None, run_dir: Path) -> list[str]:
    """Write the set-up (the audit hook and the helpers a script may call) and the code into run_dir; the command
    that runs them, in the workspace's folder with _env(run_dir, run_dir / "tmp")."""
    (run_dir / "tmp").mkdir(parents=True, exist_ok=True)
    MPL_CACHE.mkdir(parents=True, exist_ok=True)
    tracked = {k: ws.entry(k)["file"] for k in ws.ids()}
    protected = [str(p.relative_to(ws.root)).replace("\\", "/") for p in ws.protected()]  # the user's files, agent.md
    script = str((run_dir / SCRIPT).resolve())
    (run_dir / CELL).write_text(PRELUDE.format(ws=str(ws.root.resolve()), script=script, name=name or script,
                                               images=tuple(IMAGES), tracked=tracked, protected=protected,
                                               mpl=str(MPL_CACHE.resolve())),
                                encoding="utf-8")
    (run_dir / SCRIPT).write_text(code, encoding="utf-8")
    return [sys.executable, "-I", "-X", "utf8", str((run_dir / CELL).resolve())]


def own_errors(errors: str, ws: Workspace, run_dir: Path) -> str:
    """A script's error output as the script's own: the set-up's frames and the folders' paths taken out - and
    matplotlib's notice that it is building its font cache (a slow first build, not an error)."""
    errors = OWN_FRAME.sub("", MPL_NOTICE.sub("", errors))
    return errors.replace(str(ws.files.resolve()) + os.sep, "").replace(str(run_dir.resolve()) + os.sep, "")


def start(ws: Workspace, code: str, name: str, run_dir: Path) -> tuple[subprocess.Popen, dict]:
    """A program started for a console: the same set-up and rules as a run, its output unbuffered (each print and
    input() prompt comes as it is written) and its input open for the lines the user types. (the process, the
    folder as it was - for register_since when it ends). Its own run_dir: a console can run for minutes, so it
    doesn't hold the workspace's lock while it waits for the user."""
    with _lock(ws.root):
        argv = _prepare(ws, code, name, run_dir)
        before = _snapshot(ws.files)
        proc = subprocess.Popen([argv[0], "-u", *argv[1:]], cwd=ws.files, env=_env(run_dir, run_dir / "tmp"),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_new_group())
    return proc, before


def register_since(ws: Workspace, before: dict) -> tuple[list[dict], list[dict]]:
    """What a program wrote while it ran (_register), once it has ended - by path, so a helper's run at the same
    time changes the same files, never copies of them."""
    with _lock(ws.root):
        return _register(ws, before)


def stop(proc: subprocess.Popen) -> None:
    _kill_tree(proc)


def _invalid(message: str, field: str) -> ToolResult:
    return ToolResult("invalid_arguments", {"error": {"message": message, "field": field}})


def _env(home: Path, tmp: Path) -> dict:
    """Only what Python needs - nothing from the app's environment (API keys, tokens) leaks in."""
    env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("SystemRoot", r"C:\Windows")]),
           "HOME": str(home), "USERPROFILE": str(home), "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
           "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(MPL_CACHE), "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1",  # no __pycache__ folders among the user's files
           # n_jobs=-1 (scikit-learn, joblib) runs in this process instead of starting worker programs.
           "JOBLIB_MULTIPROCESSING": "0"}
    if sys.platform == "win32":
        env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
        # Pillow finds a font named 'arial.ttf' in WINDIR\fonts (measured: without it every named font fell back to
        # the default, and the helper spent four runs looking for fonts).
        env["WINDIR"] = os.environ.get("WINDIR", env["SystemRoot"])
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
# The internet refused (measured: told "refused access to files on the user's computer", the coder tried to
# download the same CSV again another way, until the user stopped the turn).
NO_INTERNET = ("Scripts here have no internet: nothing can be downloaded or fetched, in any way. A file from the "
               "web comes into the workspace through main's files(mode='download'), with the user's approval, and "
               "a script then reads it by its name. Don't try again: say so in your answer.")
NO_SUCH_FILE = re.compile(r"(?:FileNotFoundError|No such file or directory)[^\n]*?'([^'\n]+)'")
SQL_TABLE = re.compile(r"Table with name (\S+) does not exist")


def _hint(errors: str, ws: Workspace) -> str:
    """The fix for mistakes small models repeat run after run: a file name that isn't in the workspace (the
    closest ones said), a table given to duckdb by name, a module that isn't installed; and what to do when
    the script reached for the computer's files."""
    if "EOFError" in errors and "input(" in errors:  # the program asked for typed input and had none
        return (" The program waits for typed input (input()): run it again with input = what a person would type, "
                "one line per input() call, e.g. input='w\\nw\\nq'.")
    if "[refused] the internet" in errors:
        return " " + NO_INTERNET
    if "files on the user's computer are off limits" in errors:
        return (" Scripts can't read or change files on the user's computer, start programs or list drives - "
                "only the workspace's files and files written here. Tell the user that instead of trying again.")
    m = MISSING_MODULE.search(errors)
    if m and m[1] not in INSTALLED:  # measured: the coder retried the same import four times
        return (f" {m[1]} is not installed here and nothing can be installed: don't import it again. Use the "
                f"standard library or {', '.join(INSTALLED)}.")
    m = SQL_TABLE.search(errors)
    if m:  # duckdb inside a script doesn't see the workspace (query_data does)
        return (" In a script, read a table with df = pd.read_csv('its name') or table('its name') and query the "
                "DataFrame (duckdb.sql('SELECT ... FROM df')).")
    m = NO_SUCH_FILE.search(errors)
    if m and not os.path.isabs(m[1]):  # measured: a picture opened as 'image_27.jpg' instead of its name
        try:
            ws.resolve(m[1], ("table", "image", "file"))
        except ToolError as missing:
            return f" {missing.message} Open a workspace file by its name, as the workspace lists it."
    return ""


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n... ({len(text) - MAX_OUTPUT:,} characters cut) ...\n{text[-half:]}"


def _snapshot(folder: Path) -> dict[Path, tuple[int, int]]:
    """Every file in the workspace's folder, by size and change time (hidden and temporary files left out)."""
    out = {}
    for p in folder.rglob("*"):
        rel = p.relative_to(folder).parts
        if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in rel):
            st = p.stat()
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def _register(ws: Workspace, before: dict) -> tuple[list[dict], list[dict]]:
    """(what the model is told: each file the script wrote, by path; what the UI gets: the same with keys).
    New files become workspace files and changed ones their new content, through the one write path; a new
    file over the size limit, or past the count, is not kept."""
    after = _snapshot(ws.files)
    changed = sorted(p for p, sig in after.items() if before.get(p) != sig)
    told, made = [], []
    for n, p in enumerate(changed):
        rel = p.relative_to(ws.files).as_posix()
        known = ws.find_path(rel)
        big = p.stat().st_size > MAX_FILE_BYTES
        if known is None and (n >= MAX_NEW_FILES or big):
            p.unlink(missing_ok=True)  # the script's own new file: nothing of the user's
            told.append({"path": rel, "note": "not kept: larger than 20 MB" if big
                         else f"not kept: at most {MAX_NEW_FILES} new files a run"})
            continue
        try:
            put = ws.adopt(rel, by="assistant", meta={"from": "run_python"})
        except (ToolError, ValueError, OSError) as exc:
            told.append({"path": rel, "note": f"not kept: {exc}"})
            continue
        kind = ws.kind(put.key)
        if is_chart(put.path) and describe_chart(ws, put.key):
            kind = "chart"  # drawn live, like make_chart's (run shows its card)
        entry = {"path": put.path, "kind": kind, **({"updated": True} if known else {})}
        if kind not in (kind_of(rel), "chart"):
            entry["note"] = f"not a readable {kind_of(rel)}; kept as a file"
        told.append(entry)
        made.append({"path": put.path, "id": put.key, "kind": kind, **({"chart": True} if kind == "chart" else {}),
                     **({"updated": True} if known else {})})
        if known:
            folder = ws.zen / "versions" / known
            if folder.exists():
                prune_versions(folder)
    return told, made
