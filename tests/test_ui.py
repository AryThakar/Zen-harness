"""Smoke tests: run the Streamlit app against a real API server and check every page renders."""

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
import uvicorn  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from app.db.connection import Database  # noqa: E402
from app.main import create_app  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")
TIMEOUT = 120


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(Database(), tmp_path_factory.mktemp("staging")), host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    os.environ["BIZ_API_URL"] = url
    if "client" in sys.modules:  # imported by an earlier run of the app
        sys.modules["client"].API_URL = url
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def run(at: AppTest) -> AppTest:
    at.run(timeout=TIMEOUT)
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_pages_render_empty_then_with_demo_data(api):
    at = run(AppTest.from_file(APP, default_timeout=TIMEOUT))
    assert at.header[0].value == "Dashboard"
    assert any("No sales data yet" in i.value for i in at.info)

    next(b for b in at.button if b.label == "Load demo data").click()
    run(at)
    labels = [m.label for m in at.metric]
    assert labels[:4] == ["Revenue", "Orders", "Avg order value", "Profit"]
    assert all(m.value != "—" for m in at.metric)

    dimension = at.selectbox(key="dash_dimension")
    assert "category" not in dimension.options and "product" not in dimension.options  # already charted
    dimension.set_value("payment_mode")
    run(at)

    at.selectbox(key="dash_preset").set_value("All time")
    run(at)
    assert any("no % changes" in c.value for c in at.caption)  # nothing before the first day

    at.sidebar.radio(key="page").set_value("Upload data")
    run(at)
    assert at.header[0].value == "Upload data"

    at.sidebar.radio(key="page").set_value("Explore data")
    run(at)
    assert at.header[0].value == "Explore data"
    assert [t.label for t in at.tabs] == ["Sales rows", "Uploads", "Columns", "Saved formats"]
