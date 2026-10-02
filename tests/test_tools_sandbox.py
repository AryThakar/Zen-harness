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
    assert "No table 'data_9'" in run_python(ws, "table('data_9')").result["errors"]


def test_no_secrets_reach_the_script(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("POLLINATIONS_TOKEN", "tok-secret")
    r = run_python(ws, "import os\nprint(sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k))")
    assert r.result["output"].strip() == "[]"


def test_time_limit_kills_the_whole_tree(ws, tmp_path):
    marker = tmp_path / "child_was_alive.txt"
    code = ("import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3); open(\"{marker.as_posix()}\", \"w\").write(\"x\")'])\n"
            "time.sleep(60)")
    started = time.perf_counter()
    r = run_python(ws, code, timeout=1)
    assert r.status == "failed" and "time limit" in r.result["error"]["message"]
    assert time.perf_counter() - started < 15
    time.sleep(4)
    assert not marker.exists()  # the child the script started was killed too


def test_written_files_are_registered(ws):
    code = ("import pandas as pd\n"
            "pd.DataFrame({'a': [1, 2], 'b': ['x', 'y']}).to_csv('result.csv', index=False)\n"
            "open('plot.png', 'wb').write(bytes.fromhex('89504e470d0a1a0a'))\n"
            "open('notes.txt', 'w').write('hello')")
    r = run_python(ws, code)
    files = {f["path"]: f.get("id") for f in r.result["files"]}
    assert files == {"notes.txt": "file_1", "plot.png": "image_1", "result.csv": "data_2"}
    assert ws.table("data_2")["a"].tolist() == [1, 2]
    again = run_python(ws, "print('nothing new')")
    assert "files" not in again.result  # unchanged files are not registered twice


def test_long_output_is_cut_and_bad_input_refused(ws):
    r = run_python(ws, "print('x' * 50_000)")
    assert len(r.result["output"]) < 11_000 and "characters cut" in r.result["output"]
    assert run_python(ws, "   ").status == "invalid_arguments"
