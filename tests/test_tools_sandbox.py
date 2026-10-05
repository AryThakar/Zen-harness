"""run_python - direct tests (each runs a real Python process)."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from app.tools.sandbox import run_python
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
    files = {f["path"]: f.get("id") for f in r.result["files"]}
    assert files == {"broken.png": "file_1", "notes.txt": "file_2", "plot.png": "image_1", "result.csv": "data_2"}
    assert ws.lines()["image_1"].startswith("image_1  plot.png - image 40x30")  # an image item with its size
    assert [f["note"] for f in r.result["files"] if f["path"] == "broken.png"] == ["not a readable image; kept as a file"]
    assert ws.table("data_2")["a"].tolist() == [1, 2]
    again = run_python(ws, "print('nothing new')")
    assert "files" not in again.result  # unchanged files are not registered twice


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
           "import shutil; shutil.move(P, 'here.txt')", "import shutil; shutil.copy('script.py', P)",
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


@pytest.mark.parametrize("change", ["os.remove(path('file_1'))", "os.rename(path('file_1'), 'mine.txt')",
                                    "open('../registry.json', 'w').write('{}')"])
def test_stored_items_are_not_deleted_by_scripts(ws, change):
    fid = ws.add_bytes("file", b"stored", "txt", name="stored.txt")
    r = run_python(ws, f"import os\n{change}")
    assert r.status == "invalid_arguments" and "deleting needs the user's approval" in r.result["errors"]
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


def test_a_socket_pair_inside_the_script_still_works(ws):
    """Windows builds a socket pair through a loopback connect: an event loop must still run."""
    r = run_python(ws, "import asyncio, socket\na, b = socket.socketpair(); a.send(b'ok'); print(b.recv(2))\n"
                       "print(asyncio.run(asyncio.sleep(0, result='loop ok')))")
    assert r.status == "ok" and "b'ok'" in r.result["output"] and "loop ok" in r.result["output"], r.result
