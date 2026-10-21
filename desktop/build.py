# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Builds the Windows folder Zen-harness.exe runs from, and its zip - offline, from this computer:

    .venv/Scripts/python.exe desktop/build.py            (then dist/Zen-harness/Zen-harness.exe)
    .venv/Scripts/python.exe desktop/build.py --zip      (and dist/Zen-harness-<version>-windows-x64.zip)

    dist/Zen-harness/
        Zen-harness.exe desktop/Zen.cs, compiled with the C# compiler every Windows has (.NET Framework 4)
        settings.env    the model's address and password (Zen-harness.exe asks for them the first time) and other settings
        runtime/        the Python this venv was made from (the interpreter and its standard library) and the libraries
                        Zen needs: the files pip installed for them, copied from this venv - the versions the tests pass
                        with, nothing downloaded. The code sandbox runs its scripts with this Python too.
        zen/            app/, web/ and kaggle/ (the notebook and the bridge to it)
        samples/        the sample tables, to try Zen on
        README.md, LICENSE, THIRD_PARTY_NOTICES.md
        tools/          node.exe and Playwright MCP: the browser Zen reads blocked pages with and tries its games in
                        (Edge itself is Windows' own)

Nothing of the user's goes in: no .env, no data/, no bridge.env - the folder is safe to share.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist" / "Zen-harness"
PROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
PLAYWRIGHT_MCP = Path(os.environ.get("ASSISTANT_PLAYWRIGHT_MCP", REPO.parent / "playwright-mcp"))  # beside the repo

# The base Python's own parts left out: tests, IDLE and Tk (nothing of Zen's draws a Tk window), debug and
# free-threaded builds, headers - a quarter of its size
SKIP_LIB = {"site-packages", "test", "idlelib", "tkinter", "turtledemo", "ensurepip", "__pycache__", "lib2to3"}
SKIP_FILE = re.compile(r"(_d\.(exe|dll|pyd)|\.pdb|\dt\.(exe|dll)|_tkinter\.pyd|tcl\d+t?\.dll|tk\d+t?\.dll"
                       r"|\.cp\d+t-win_amd64\.pyd)$", re.I)
SETTINGS = """\
# Zen harness settings - NAME=value lines, read by Zen-harness.exe each time it starts.
# The model: the Kaggle notebook (zen\\kaggle\\zen-qwen36-server.ipynb) prints these two in its last cell.
# Zen-harness.exe asks for them the first time, and again when the notebook has a new address.
# Leave them empty to use a llama-server running on this computer at 127.0.0.1:8080.
KAGGLE_URL=
KAGGLE_PASSWORD=

# Your name, as Zen greets you
# ASSISTANT_USER_NAME=

# Pictures: a free key from https://enter.pollinations.ai (without one, Zen can't make pictures)
# POLLINATIONS_KEY=
"""


def needed() -> list[metadata.Distribution]:
    """The installed distributions Zen needs: its dependencies and the sandbox's, and theirs, through pip's own
    metadata (the first product's leftovers in this venv - streamlit and the like - are not among them)."""
    norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
    env = default_environment()
    todo = [(Requirement(r), ("",)) for r in PROJECT["dependencies"] + PROJECT["optional-dependencies"]["sandbox"]]
    seen, found = set(), {}
    while todo:
        req, extras = todo.pop()
        if req.marker and not any(req.marker.evaluate({**env, "extra": e}) for e in extras):
            continue
        key = (norm(req.name), tuple(sorted(req.extras)))
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            sys.exit(f"{req.name} is needed but not installed in this venv: pip install -r requirements.lock -e .")
        found[norm(req.name)] = dist
        todo += [(Requirement(r), tuple(req.extras) or ("",)) for r in dist.requires or []]
    return sorted(found.values(), key=lambda d: d.metadata["Name"].lower())


def copy_runtime() -> int:
    base, site = Path(sys.base_prefix), Path(sysconfig.get_paths()["purelib"]).resolve()
    out = DIST / "runtime"
    for item in base.iterdir():
        if item.is_file() and item.suffix.lower() in (".exe", ".dll", ".txt") and not SKIP_FILE.search(item.name):
            shutil.copy2(item, out / item.name)
    for part in ("DLLs", "Lib"):
        for path in (base / part).rglob("*"):
            rel = path.relative_to(base)
            if path.is_dir() or any(p in SKIP_LIB for p in rel.parts[1:]) or SKIP_FILE.search(path.name):
                continue
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out / rel)
    count = 0
    target = out / "Lib" / "site-packages"
    for dist in needed():
        if dist.files is None:
            sys.exit(f"{dist.metadata['Name']} has no record of its files: reinstall it with pip")
        for f in dist.files:
            src = Path(dist.locate_file(f)).resolve()
            if site not in src.parents or "__pycache__" in src.parts or not src.is_file():
                continue  # a script in Scripts/, or a cache
            dst = target / src.relative_to(site)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        count += 1
    return count


def copy_code() -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "bridge.env")
    for part in ("app", "web", "kaggle"):
        shutil.copytree(REPO / part, DIST / "zen" / part, ignore=ignore)
    shutil.copytree(REPO / "data" / "samples", DIST / "samples", ignore=ignore)
    for doc in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(REPO / doc, DIST / doc)


def copy_tools() -> None:
    node = shutil.which("node")
    if node:
        (DIST / "tools" / "node").mkdir(parents=True)
        shutil.copy2(node, DIST / "tools" / "node" / "node.exe")
    if (PLAYWRIGHT_MCP / "node_modules" / "@playwright" / "mcp").is_dir():
        shutil.copytree(PLAYWRIGHT_MCP, DIST / "tools" / "playwright-mcp", ignore=shutil.ignore_patterns(".cache"))
    else:
        print("no Playwright MCP at", PLAYWRIGHT_MCP, "- Zen will run without its browser")


def compile_launcher() -> None:
    csc = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    done = subprocess.run([str(csc), "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu", "/codepage:65001",
                           f"/win32icon:{REPO / 'desktop' / 'zen.ico'}", f"/out:{DIST / 'Zen-harness.exe'}",
                           "/r:System.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
                           str(REPO / "desktop" / "Zen.cs")], capture_output=True, text=True)
    if done.returncode:
        sys.exit("Zen.cs did not compile:\n" + done.stdout + done.stderr)


def smoke_test() -> None:
    """The copied Python imports the app from the folder, alone (no venv, no repository on its path)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
    done = subprocess.run([str(DIST / "runtime" / "python.exe"), "-c",
                           "import app.main, pandas, statsforecast, sklearn, matplotlib, duckdb, fastembed, uvicorn; print('ok')"],
                          cwd=DIST / "zen", env=env, capture_output=True, text=True)
    if done.stdout.strip() != "ok":
        sys.exit("the built runtime can't import Zen:\n" + done.stdout + done.stderr)


def size(path: Path) -> str:
    return f"{sum(f.stat().st_size for f in path.rglob('*') if f.is_file()) / 1e6:,.0f} MB"


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)  # the build's own output, made again from scratch
    (DIST / "runtime").mkdir(parents=True)
    libraries = copy_runtime()
    copy_code()
    copy_tools()
    compile_launcher()
    (DIST / "settings.env").write_text(SETTINGS, encoding="utf-8")
    smoke_test()
    print(f"built {DIST}: {size(DIST)} (runtime {size(DIST / 'runtime')}, {libraries} libraries)")
    if "--zip" in sys.argv:
        archive = DIST.parent / f"Zen-harness-{PROJECT['version']}-windows-x64.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(DIST.rglob("*")):
                if f.is_file():
                    z.write(f, Path(DIST.name) / f.relative_to(DIST))
        print(f"zipped {archive}: {archive.stat().st_size / 1e6:,.0f} MB")


if __name__ == "__main__":
    main()
