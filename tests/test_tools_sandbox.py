# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""run_python - direct tests (each runs a real Python process)."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from app.tools.sandbox import NO_INTERNET, run_python
from app.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "ws")
    w.add_table(pd.DataFrame({"x": [1, 2, 3], "y": [10.0, 20.0, 30.0]}), "small")
    return w


def test_runs_code_and_reads_workspace_tables(ws):
    r = run_python(ws, "df = table('data_1')\nprint(df['y'].sum())\ndf.loc[0, 'y'] = -1")
    assert r.status == "ok" and r.result["output"].strip() == "60.0" and r.result["exit_code"] == 0
    assert ws.table("data_1")["y"].tolist() == [10.0, 20.0, 30.0]  # a copy: the original is unchanged


def test_errors_come_back_with_output_so_far(ws):
    r = run_python(ws, "print('before')\n1 / 0")
    assert r.status == "invalid_arguments" and r.result["output"].strip() == "before"
    assert "ZeroDivisionError" in r.result["errors"]
    # The script's own line numbers, without the set-up code's frames.
    assert 'File "script.py", line 2' in r.result["errors"] and "_cell.py" not in r.result["errors"]


def test_a_new_workspace_with_nothing_stored_runs_scripts(tmp_path):
    r = run_python(Workspace(tmp_path / "new"), "open('a.txt', 'w').write('hi')\nprint(open('a.txt').read())")
    assert r.status == "ok" and r.result["output"].strip() == "hi"  # no registry yet: every open() failed


def test_open_figures_are_saved_once(ws):
    plot = "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [1, 4, 9])\n"
    shown = run_python(ws, plot + "plt.show()").result  # what small models write: a figure left open
    assert [f["path"] for f in shown["files"]] == ["figure_1.png"] and "errors" not in shown
    saved = run_python(ws, plot + "plt.savefig('curve.png')").result  # saved by the script: no second copy
    assert [f["path"] for f in saved["files"]] == ["curve.png"]
    # a working script saving under work/ saved it too (measured: figure_1.png came beside the MRF charts), and one
    # that leaves its figure open gets it in its own folder
    (ws.files / "work").mkdir(exist_ok=True)  # there once a script is written under it
    in_work = run_python(ws, plot + "plt.savefig('work/line.png')", name="work/charts.py").result
    assert [f["path"] for f in in_work["files"]] == ["work/line.png"]
    left_open = run_python(ws, plot + "plt.show()", name="work/look.py").result
    assert [f["path"] for f in left_open["files"]] == ["work/figure_1.png"]
    # a run that failed leaves no picture: its figure is half drawn (measured: empty axes shown as figure_1.png)
    crashed = run_python(ws, "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\n1 / 0", name="work/bad.py")
    assert crashed.result.get("files", []) == [] and "ZeroDivisionError" in crashed.result["errors"]
    ended = run_python(ws, plot + "import sys\nsys.exit(0)", name="ends.py").result  # sys.exit(0) finished it
    assert [f["path"] for f in ended["files"]] == ["figure_1.png"]


def test_matplotlib_builds_its_font_cache_once_and_its_notice_is_no_error(ws, tmp_path):
    """Measured 2026-10-21: each run had its own font cache, built again (1-4 s); past 5 s on a busy machine,
    matplotlib's notice on stderr made a finished chart read as an error."""
    from app.tools.sandbox import MPL_CACHE, _env, own_errors
    assert _env(tmp_path / "a", tmp_path / "a" / "tmp")["MPLCONFIGDIR"] == _env(tmp_path / "b", tmp_path / "b" / "tmp")["MPLCONFIGDIR"] == str(MPL_CACHE)
    noise = "Matplotlib is building the font cache; this may take a moment.\n"
    assert own_errors(noise, ws, tmp_path) == ""
    assert own_errors(noise + "ValueError: bad\n", ws, tmp_path) == "ValueError: bad\n"
    drawn = run_python(ws, "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.savefig('a.png')").result
    assert "errors" not in drawn and any(MPL_CACHE.glob("fontlist*.json"))  # the shared cache, written there


def test_a_missing_module_is_said_once_with_what_is_installed(ws):
    from app.tools.sandbox import INSTALLED
    message = run_python(ws, "import plotting_lib_xyz").result["error"]["message"]
    assert "plotting_lib_xyz is not installed" in message and "matplotlib" in message
    ok = run_python(ws, "import " + ", ".join(INSTALLED))  # the list it gives is true
    assert ok.status == "ok", ok.result.get("errors")
    assert "No table 'data_9'" in run_python(ws, "table('data_9')").result["errors"]


def test_no_secrets_reach_the_script(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("POLLINATIONS_TOKEN", "tok-secret")
    r = run_python(ws, "import os\nprint(sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k))")
    assert r.result["output"].strip() == "[]"


def test_time_limit_stops_the_script_and_children_are_never_started(ws, tmp_path):
    started = time.perf_counter()
    r = run_python(ws, "import time\ntime.sleep(60)", timeout=1)
    assert r.status == "failed" and "time limit" in r.result["error"]["message"]
    assert time.perf_counter() - started < 15
    marker = tmp_path / "child_was_alive.txt"  # starting programs is refused (the tree kill stays as a backstop)
    code = ("import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', 'open(\"{marker.as_posix()}\", \"w\").write(\"x\")'])")
    r = run_python(ws, code)
    time.sleep(2)
    assert "Starting programs is not allowed" in r.result["errors"] and not marker.exists()


def test_written_files_are_registered(ws):
    code = ("import pandas as pd\n"
            "from PIL import Image\n"
            "pd.DataFrame({'a': [1, 2], 'b': ['x', 'y']}).to_csv('result.csv', index=False)\n"
            "Image.new('RGB', (40, 30), 'teal').save('plot.png')\n"
            "open('broken.png', 'wb').write(bytes.fromhex('89504e470d0a1a0a'))\n"
            "open('notes.txt', 'w').write('hello')")
    r = run_python(ws, code)
    told = {f["path"]: f["kind"] for f in r.result["files"]}  # the model is told paths, never keys
    assert told == {"broken.png": "file", "notes.txt": "file", "plot.png": "image", "result.csv": "table"}
    assert "_1" not in str(r.result["files"])
    keys = {f["path"]: f["id"] for f in r.artifact["files"]}  # the UI gets the keys
    assert ws.lines()[keys["plot.png"]] == "plot.png: image 40x30"  # an image with its size
    assert [f["note"] for f in r.result["files"] if f["path"] == "broken.png"] == ["not a readable image; kept as a file"]
    assert ws.table(keys["result.csv"])["a"].tolist() == [1, 2]
    assert (ws.files / "result.csv").exists()  # where the script wrote it: the file is the workspace's
    again = run_python(ws, "print('nothing new')")
    assert "files" not in again.result  # unchanged files are not registered twice


def test_a_script_opens_files_by_their_names(ws):
    r = run_python(ws, "import pandas as pd\nprint(pd.read_csv('small.csv')['y'].sum(), table('small.csv')['x'].sum())")
    assert r.status == "ok" and r.result["output"].split() == ["60.0", "6"]
    missing = run_python(ws, "open('smal.csv').read()")
    assert "Did you mean: small.csv" in missing.result["error"]["message"]  # measured: 'image_27.jpg' for a picture


def test_a_file_the_assistant_made_changes_with_a_version_kept(ws):
    key = ws.add_bytes("file", b"v1", "txt", name="draft.txt")
    r = run_python(ws, "open('draft.txt', 'w').write('v2')")
    assert r.status == "ok" and r.result["files"] == [{"path": "draft.txt", "kind": "file", "updated": True}]
    assert ws.path(key).read_bytes() == b"v2" and ws.ids("file") == [key]  # the same file, not a copy
    assert [(ws.zen / "versions" / key / v["version"]).read_bytes() for v in ws.versions(key)] == [b"v1"]


def test_the_users_own_file_is_never_changed_by_a_script(ws, tmp_path):
    upload = tmp_path / "mine.csv"
    upload.write_text("a\n1\n")
    key = ws.add_file(upload)
    r = run_python(ws, "open('mine.csv', 'w').write('a,2')")
    assert r.status == "invalid_arguments" and "the user's own file" in r.result["errors"]
    assert ws.path(key).read_text() == "a\n1\n" and ws.versions(key) == []


def test_long_output_is_cut_and_bad_input_refused(ws):
    r = run_python(ws, "print('x' * 50_000)")
    assert len(r.result["output"]) < 11_000 and "characters cut" in r.result["output"]
    assert run_python(ws, "   ").status == "invalid_arguments"


def test_files_on_the_computer_are_off_limits(ws):
    for code in ('open(r"C:\\Windows\\win.ini").read()', 'import os; os.listdir("C:\\\\")',
                 'import subprocess; subprocess.run(["cmd", "/c", "dir"])'):
        r = run_python(ws, code)
        assert r.status == "invalid_arguments" and "off limits" in r.result["errors"], code
    walked = run_python(ws, 'import os\nprint(sum(len(f) for _, _, f in os.walk("C:/Users")))')
    assert walked.status == "ok" and walked.result["output"].strip() == "0"  # os.walk hides the refusal ...
    assert "not real" in walked.result["note"]  # ... so the result says so
    fine = run_python(ws, 'import os\nopen("a.txt", "w").write("x"); os.remove("a.txt"); print(table("data_1").shape)')
    assert fine.status == "ok" and "note" not in fine.result


# Every way a script could change or delete a file (the user's rule: only the workspace's own files).
CHANGES = ["open(P, 'w').write('x')", "open(P, 'a').write('x')", "import os; os.remove(P)", "import os; os.unlink(P)",
           "import os; os.rename(P, P + '.moved')", "import os; os.replace(P, 'here.txt')",
           "import pathlib; pathlib.Path(P).unlink()", "import pathlib; pathlib.Path(P).write_text('x')",
           "import shutil; shutil.move(P, 'here.txt')", "open('x.txt', 'w').write('x'); import shutil; shutil.copy('x.txt', P)",
           "import shutil; shutil.rmtree(D)", "import os; os.chmod(P, 0o444)", "import os; os.truncate(P, 0)",
           "import os; os.mkdir(D + '/new')", "import os; os.utime(P)",
           "import os; os.close(os.open(P, os.O_WRONLY | os.O_TRUNC))"]


@pytest.mark.parametrize("change", CHANGES)
def test_files_outside_the_workspace_cannot_be_changed_or_deleted(ws, tmp_path, change):
    outside = tmp_path / "users_files"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("mine")
    r = run_python(ws, f"P = {str(keep)!r}\nD = {str(outside)!r}\n{change}")
    assert r.status == "invalid_arguments" and "off limits" in r.result["errors"], r.result
    assert keep.read_text() == "mine" and [p.name for p in outside.iterdir()] == ["keep.txt"]


@pytest.mark.parametrize("change, said", [("os.remove('stored.txt')", "files(delete) asks the user"),
                                          ("os.rename('stored.txt', 'mine.txt')", "files(rename) moves one"),
                                          ("open('../.zen/registry.json', 'w').write('{}')", "off limits")])
def test_stored_files_are_not_deleted_by_scripts(ws, change, said):
    fid = ws.add_bytes("file", b"stored", "txt", name="stored.txt")
    r = run_python(ws, f"import os\n{change}")
    assert r.status == "invalid_arguments" and said in r.result["errors"]
    assert ws.path(fid).read_bytes() == b"stored" and fid in ws.ids()


# Every way a script could reach the network (the user's rule: files come in only through the approval card).
NETWORK = ["import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)",
           "import socket; socket.getaddrinfo('example.com', 443)",
           "import socket; socket.create_connection(('93.184.216.34', 80), timeout=5)",
           "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'x', ('8.8.8.8', 53))",
           "import http.client; http.client.HTTPConnection('127.0.0.1', 8080, timeout=5).request('GET', '/health')",
           "import socket; socket.create_connection(('localhost', 8000), timeout=5)"]


@pytest.mark.parametrize("call", NETWORK)
def test_scripts_have_no_network(ws, call):
    """Measured: a coder script downloaded a CSV with requests, without the user's approval card."""
    r = run_python(ws, call)
    assert r.status == "invalid_arguments" and "no internet access" in r.result["errors"], r.result
    # the reason is the internet, not files on the computer, and the way in is said: main's files(download)
    assert r.result["note"] == NO_INTERNET and "files(mode='download')" in r.result["error"]["message"]


def test_a_script_that_hides_the_refused_download_still_hears_why(ws):
    """Measured: the coder wrapped the download in try/except, the run 'passed' printing the error, and the note
    said it was refused files on the user's computer - so it tried again another way."""
    r = run_python(ws, "import urllib.request\ntry:\n    urllib.request.urlopen('https://example.com/a.csv', timeout=5)\n"
                       "except Exception as e:\n    print('An error occurred:', e)\n")
    assert r.status == "ok" and r.result["note"] == NO_INTERNET and "computer" not in r.result["note"]


def test_a_socket_pair_inside_the_script_still_works(ws):
    """Windows builds a socket pair through a loopback connect: an event loop must still run."""
    r = run_python(ws, "import asyncio, socket\na, b = socket.socketpair(); a.send(b'ok'); print(b.recv(2))\n"
                       "print(asyncio.run(asyncio.sleep(0, result='loop ok')))")
    assert r.status == "ok" and "b'ok'" in r.result["output"] and "loop ok" in r.result["output"], r.result
