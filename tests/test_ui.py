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

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.agent.providers import ModelTurn, ScriptedProvider  # noqa: E402
from app.agent.providers.scripted import call  # noqa: E402
from app.db.connection import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.rag.embed import HashEmbedder  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")
TIMEOUT = 120
PROVIDER = ScriptedProvider([])  # tests queue the model's turns on it


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Database(), tmp_path_factory.mktemp("staging"), provider=PROVIDER,
                     checkpointer=InMemorySaver(), embedder=HashEmbedder())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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

    at.sidebar.radio(key="page").set_value("Insights")
    run(at)
    assert at.header[0].value == "Insights"
    assert any("No forecast yet" in i.value for i in at.info)
    next(b for b in at.button if b.label == "Run forecast").click()
    run(at)
    assert [m.label for m in at.metric][:4] == ["Expected revenue", "Backtest error", "Baseline error", "Range held"]
    next(b for b in at.button if b.label == "Find segments").click()
    run(at)
    next(b for b in at.button if b.label == "Check for unusual days").click()
    run(at)
    next(b for b in at.button if b.label == "Predict churn").click()
    run(at)
    assert {"Likely to stop buying", "Ranking quality (AUC)", "Found in top 20%"} <= {m.label for m in at.metric}
    next(b for b in at.button if b.label == "Find products bought together").click()
    run(at)
    assert at.selectbox(key="basket_item").value  # a product with companions is preselected
    at.selectbox(key="basket_item").set_value("Smartphone X1")
    run(at)
    assert any(h.value == "Bought with Smartphone X1" for h in at.subheader)
    assert not at.error

    at.sidebar.radio(key="page").set_value("Documents")
    run(at)
    assert any("No documents yet" in i.value for i in at.info)
    next(b for b in at.button if b.label == "Load sample documents").click()
    run(at)
    assert len(at.expander) == 4
    at.text_input(key="docs_query").set_value("UPI refund")
    run(at)
    assert any('returns_and_warranty_policy.md - "Refunds"' in m.value for m in at.markdown)
    assert not at.error

    at.sidebar.radio(key="page").set_value("Upload data")
    run(at)
    assert at.header[0].value == "Upload data"

    at.sidebar.radio(key="page").set_value("Explore data")
    run(at)
    assert at.header[0].value == "Explore data"
    assert [t.label for t in at.tabs] == ["Sales rows", "Uploads", "Columns", "Saved formats"]


def test_ask_ai_streams_steps_charts_and_remembers(api):
    at = run(AppTest.from_file(APP, default_timeout=TIMEOUT))
    at.sidebar.radio(key="page").set_value("Ask AI")
    run(at)
    assert at.header[0].value == "Ask AI"
    assert at.chat_input(key="chat_input").disabled is False

    PROVIDER.turns.extend([
        ModelTurn(text="Let me check.", stop="tool_use", tool_calls=[
            call("run_sql", sql="SELECT product, sum(revenue) AS revenue FROM sales GROUP BY 1 ORDER BY 2 DESC LIMIT 5"),
            call("make_chart", chart_type="bar", unit="money", title="Top products", x="product", y="revenue",
                 sql="SELECT product, sum(revenue) AS revenue FROM sales GROUP BY 1 ORDER BY 2 DESC LIMIT 5"),
            call("search_docs", query="return opened phone"),
            call("churn_risk", horizon_days=90, limit=5),
        ]),
        ModelTurn(text="Your top product earns the most; see the chart."),
    ])
    next(b for b in at.button if b.label == "Can customers return an opened phone?").click()
    run(at)
    assert not at.error
    texts = [m.value for m in at.markdown]
    assert "**Top products**" in texts
    assert "**Customers most likely to stop buying (next 90 days)**" in texts  # a titled table, next to the answer
    assert any("Your top product earns the most" in t for t in texts)

    run(at)  # a rerun redraws the conversation from the saved thread
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    texts = [m.value for m in at.markdown]
    assert any("Queried the data" in t for t in texts) and any("Searched your documents" in t for t in texts)
    assert any("*returns_and_warranty_policy.md" in t for t in texts)  # the passage it found, with its source

    PROVIDER.turns.append(ModelTurn(text="It costs $5 to ship."))
    at.chat_input(key="chat_input").set_value("And shipping?")
    run(at)
    assert any("It costs \\$5 to ship." in m.value for m in at.markdown)  # $ escaped, no LaTeX
    assert len(PROVIDER.requests[-1]["messages"]) == 8  # the first exchange (question, 4 tool calls, answer) was sent along

    next(b for b in at.button if b.label == "New conversation").click()
    run(at)
    assert not at.chat_message and any(b.label.startswith("Forecast revenue") for b in at.button)
