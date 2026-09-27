"""The AI assistant: SQL guard, tools, Claude adapter, graph and chat API."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import sql_guard
from app.agent.graph import MAX_TOOL_ROUNDS, build_graph, new_question
from app.agent.providers import ModelTurn, ProviderError, ScriptedProvider
from app.agent.providers.claude import ClaudeProvider, parse_message, to_anthropic_messages
from app.agent.providers.scripted import call
from app.agent.tools import TOOL_SPECS, ToolContext, execute
from app.db.connection import Database
from app.ingestion.pipeline import ingest_file
from app.main import create_app

from .conftest import SAMPLES, TODAY


@pytest.fixture
def db():
    database = Database()
    ingest_file(database, SAMPLES / "superstore_sample.csv", today=TODAY)
    yield database
    database.close()


# --- SQL guard -------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT category, sum(revenue) FROM sales GROUP BY 1",
    "WITH m AS (SELECT date_trunc('month', order_date) AS month, sum(revenue) AS r FROM sales GROUP BY 1) "
    "SELECT * FROM m ORDER BY month",
    "SELECT c.customer_id, s.segment FROM customers c JOIN latest_customer_segments s USING (customer_id)",
    "SELECT * FROM range(3)",
    "SELECT product FROM sales UNION SELECT product FROM products",
])
def test_guard_accepts_read_only_queries(sql):
    assert sql_guard.check(sql)


@pytest.mark.parametrize("sql, reason", [
    ("DROP TABLE sales", "read-only"),
    ("INSERT INTO sales SELECT * FROM sales", "read-only"),
    ("SELECT 1; DELETE FROM sales", "exactly one"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv"),
    ("SELECT * FROM 'secrets.csv'", "not available"),
    ("SELECT * FROM duckdb_settings()", "duckdb_settings"),
    ("SELECT * FROM information_schema.tables", "without a schema"),
    ("SELECT getenv('HOME')", "getenv"),
    ("ATTACH 'other.db'", "read-only"),
    ("PRAGMA database_list", "read-only"),
    ("COPY sales TO 'out.csv'", "read-only"),
    ("SELECT * FROM schema_migrations", "not available"),
    ("SELEC 1", "parse"),
])
def test_guard_rejects_everything_else(sql, reason):
    with pytest.raises(sql_guard.SqlRejected, match=reason):
        sql_guard.check(sql)


def test_database_itself_refuses_file_access(db):
    cur = db.cursor()
    with pytest.raises(Exception, match="(?i)permission|disabled"):
        cur.execute("SELECT * FROM read_csv('/etc/passwd')")


def test_guard_caps_rows_and_reports_the_total(db):
    result = sql_guard.run(db.cursor(), "SELECT * FROM range(500)", max_rows=10)
    assert len(result.rows) == 10 and result.truncated and result.total_rows == 500


def test_guard_times_out(db):
    with pytest.raises(sql_guard.SqlRejected, match="longer than"):
        sql_guard.run(db.cursor(), "SELECT count(*) FROM range(100000000) a, range(1000) b", timeout=0.2)


# --- tools -----------------------------------------------------------------------

def test_tool_specs_are_strict_json_schemas():
    names = [t["name"] for t in TOOL_SPECS]
    assert names == ["describe_data", "run_sql", "business_summary", "calculate", "business_metric",
                     "make_chart", "forecast", "customer_segments", "unusual_days"]
    for spec in TOOL_SPECS:
        assert spec["input_schema"]["type"] == "object"
        assert spec["input_schema"]["additionalProperties"] is False
        assert '"title": "' not in json.dumps(spec["input_schema"])  # no pydantic title annotations


def test_tools_return_text_and_artifacts(db):
    ctx = ToolContext(db, "$")
    table = execute(ctx, "run_sql", {"sql": "SELECT category, round(sum(revenue), 2) AS r FROM sales GROUP BY 1 ORDER BY 2 DESC"})
    assert not table.is_error and "Furniture | 819.55" in table.content
    assert table.artifact["kind"] == "table" and table.artifact["rows"][0] == ["Furniture", 819.55]

    chart = execute(ctx, "make_chart", {"chart_type": "bar", "x": "category", "y": "r", "unit": "money",
                                        "title": "By category",
                                        "sql": "SELECT category, sum(revenue) AS r FROM sales GROUP BY 1"})
    assert chart.artifact["kind"] == "chart" and len(chart.artifact["points"]) == 3

    summary = execute(ctx, "business_summary", {"start": "2024-01-01", "end": "2024-12-31"})
    assert "Revenue: 1,506.19" in summary.content and "Orders: 7" in summary.content

    assert execute(ctx, "calculate", {"expression": "round(819.546 / 1506.192 * 100, 1)"}).content.endswith("= 54.40")
    metric = execute(ctx, "business_metric", {"metric": "growth_pct", "values": {"old": 80, "new": 100}})
    assert "growth_pct = 25" in metric.content


@pytest.mark.parametrize("name, raw, fragment", [
    ("run_sql", {"sql": 7}, "INVALID_JSON"),
    ("run_sql", {"sql": "SELECT 1", "extra": True}, "INVALID_JSON"),
    ("run_sql", "not an object", "INVALID_JSON"),
    ("calculate", {"expression": "__import__('os').system('x')"}, "Can't evaluate"),
    ("calculate", {"expression": "1/0"}, "Division by zero"),
    ("business_metric", {"metric": "roi_pct", "values": {"gain": 5}}, "missing cost"),
    ("make_chart", {"chart_type": "line", "sql": "SELECT 1 AS a", "x": "a", "y": "b", "unit": "count", "title": "t"},
     "not in the query"),
    ("forecast", {"horizon": 7, "by": "category"}, "both"),
    ("forecast", {"horizon": 7, "by": "product", "value": "Nope"}, "needs at least"),
    ("describe_data", {"table": "secrets"}, "Unknown table"),
    ("launch_rockets", {}, "Unknown tool"),
])
def test_bad_tool_calls_become_error_results(db, name, raw, fragment):
    output = execute(ToolContext(db, "$"), name, raw)
    assert output.is_error and fragment in output.content


# --- Claude adapter --------------------------------------------------------------

def assistant(raw, calls=(), provider="anthropic"):
    return {"role": "assistant", "text": "", "tool_calls": list(calls), "raw": raw, "provider": provider}


def tool_result(call_id, content="ok", is_error=False):
    return {"role": "tool", "tool_call_id": call_id, "name": "run_sql", "content": content, "is_error": is_error}


def test_history_replays_blocks_and_groups_tool_results():
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    raw = [thinking, {"type": "text", "text": "Checking."},
           {"type": "tool_use", "id": "a", "name": "run_sql", "input": {}},
           {"type": "tool_use", "id": "b", "name": "calculate", "input": {}}]
    history = [{"role": "user", "content": "q"}, assistant(raw), tool_result("a"), tool_result("b", "bad", True)]
    out = to_anthropic_messages(history)
    assert out[1] == {"role": "assistant", "content": raw}  # replayed unchanged, thinking included
    assert out[2]["role"] == "user" and [r["tool_use_id"] for r in out[2]["content"]] == ["a", "b"]
    assert out[2]["content"][1]["is_error"] is True


def test_history_drops_tool_calls_that_never_ran():
    raw = [{"type": "text", "text": "Let me look"}, {"type": "tool_use", "id": "x", "name": "run_sql", "input": {}}]
    out = to_anthropic_messages([{"role": "user", "content": "q"}, assistant(raw), {"role": "user", "content": "q2"}])
    assert out[1]["content"] == [{"type": "text", "text": "Let me look"}]


def test_history_after_a_mid_answer_fallback():
    raw = [{"type": "thinking", "thinking": "", "signature": "s1"}, {"type": "text", "text": "Partial "},
           {"type": "tool_use", "id": "old", "name": "run_sql", "input": {}},
           {"type": "fallback", "from": {"model": "claude-opus-5"}, "to": {"model": "claude-opus-4-8"}},
           {"type": "text", "text": "rest"}, {"type": "tool_use", "id": "new", "name": "run_sql", "input": {}}]
    out = to_anthropic_messages([{"role": "user", "content": "q"}, assistant(raw), tool_result("new")])
    assert [b["type"] for b in out[1]["content"]] == ["text", "text", "tool_use"]
    assert out[1]["content"][2]["id"] == "new"


def test_empty_turns_are_skipped_and_other_providers_rebuilt():
    history = [
        {"role": "user", "content": "q1"},
        assistant([{"type": "thinking", "thinking": "", "signature": "s"}]),  # refused, nothing replayable
        {"role": "user", "content": "q2"},
        {"role": "assistant", "text": "From Ollama", "tool_calls": [], "raw": [{"x": 1}], "provider": "ollama"},
    ]
    out = to_anthropic_messages(history)
    assert [m["role"] for m in out] == ["user", "user", "assistant"]
    assert out[2]["content"] == [{"type": "text", "text": "From Ollama"}]


def block(**fields):
    return SimpleNamespace(to_dict=lambda: fields)


def test_refused_or_truncated_turns_never_run_tools():
    tool = block(type="tool_use", id="t", name="run_sql", input={"sql": "SELECT"})
    ok = parse_message(SimpleNamespace(content=[block(type="text", text="Hi"), tool], stop_reason="tool_use",
                                       model="claude-opus-5"))
    assert ok.stop == "tool_use" and ok.tool_calls[0]["id"] == "t"
    truncated = parse_message(SimpleNamespace(content=[tool], stop_reason="max_tokens", model="m"))
    assert truncated.tool_calls == [] and truncated.stop == "max_tokens"
    refused = parse_message(SimpleNamespace(content=[], stop_reason="refusal", model="m"))
    assert refused.tool_calls == [] and refused.text


class FakeStream:
    def __init__(self, events, message=None, error=None):
        self.events, self.message, self.error = events, message, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self.events
        if self.error:
            raise self.error

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, streams):
        self.streams, self.calls = list(streams), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.calls.append(params)
        return self.streams.pop(0)


def test_claude_request_settings_and_json_retry():
    final = SimpleNamespace(content=[block(type="text", text="Done")], stop_reason="end_turn", model="claude-opus-5")
    client = FakeClient([
        FakeStream([SimpleNamespace(type="text", text="Par")], error=ValueError("bad tool json")),
        FakeStream([SimpleNamespace(type="text", text="Done")], final),
    ])
    events = []
    provider = ClaudeProvider("claude-opus-5", client=client)
    turn = provider.complete("system", [{"role": "user", "content": "q"}], TOOL_SPECS[:1], allow_tools=False,
                             on_event=lambda kind, text: events.append((kind, text)))
    assert turn.text == "Done"
    assert events == [("text", "Par"), ("restart", ""), ("text", "Done")]
    params = client.calls[0]
    assert params["model"] == "claude-opus-5" and params["max_tokens"] == 64000
    assert params["betas"] == ["server-side-fallback-2026-07-01"] and params["fallbacks"] == "default"
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["tools"][0]["eager_input_streaming"] is True
    assert params["tool_choice"] == {"type": "none"}
    assert "thinking" not in params and "temperature" not in params


def test_claude_needs_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    ready, message = ClaudeProvider("claude-opus-5").ready()
    assert not ready and "ANTHROPIC_API_KEY" in message


# --- graph -----------------------------------------------------------------------

def test_graph_runs_tools_and_remembers_the_conversation(db):
    provider = ScriptedProvider([
        ModelTurn(text="Checking.", tool_calls=[call("run_sql", sql="SELECT count(*) AS n FROM sales")], stop="tool_use"),
        ModelTurn(text="There are 8 sales lines."),
        ModelTurn(text="Still 8."),
    ])
    graph = build_graph(db, provider, InMemorySaver(), currency="$")
    config = {"configurable": {"thread_id": "t"}}
    events = list(graph.stream(new_question("How many lines?"), config, stream_mode="custom"))
    kinds = [e["type"] for e in events]
    assert "tool_start" in kinds and kinds.index("tool_start") < kinds.index("tool_end")
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert tool_end["artifact"]["rows"] == [[8]]
    assert "Money is in $" in provider.requests[0]["system"] and "sales columns" in provider.requests[0]["system"]

    list(graph.stream(new_question("And now?"), config, stream_mode="custom"))
    sent = provider.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]


def test_graph_stops_tools_after_the_round_limit(db):
    endless = [ModelTurn(text="", tool_calls=[call("calculate", expression="1+1")], stop="tool_use")
               for _ in range(MAX_TOOL_ROUNDS + 1)]
    provider = ScriptedProvider(endless + [ModelTurn(text="unused")])
    graph = build_graph(db, provider, InMemorySaver())
    list(graph.stream(new_question("loop"), {"configurable": {"thread_id": "l"}}, stream_mode="custom"))
    assert [r["allow_tools"] for r in provider.requests] == [True] * MAX_TOOL_ROUNDS + [False]


# --- chat API ----------------------------------------------------------------------

def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_chat_streams_answer_steps_and_artifacts(tmp_path):
    provider = ScriptedProvider([
        ModelTurn(text="", tool_calls=[call("make_chart", chart_type="bar", title="Revenue by category", x="category",
                                            y="r", unit="money", sql="SELECT category, sum(revenue) AS r FROM sales GROUP BY 1")],
                  stop="tool_use"),
        ModelTurn(text="Furniture sells most."),
    ])
    database = Database()
    ingest_file(database, SAMPLES / "superstore_sample.csv", today=TODAY)
    with TestClient(create_app(database, tmp_path, provider=provider, checkpointer=InMemorySaver())) as client:
        assert client.get("/chat/status").json() == {"provider": "scripted", "model": "scripted", "ready": True,
                                                    "message": "Scripted test model"}
        r = client.post("/chat", json={"message": "Which category sells most?"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = sse_events(r.text)
        kinds = [k for k, _ in events]
        assert kinds[0] == "thread" and kinds[-1] == "done"
        assert "tool_start" in kinds and "tool_end" in kinds and "text" in kinds
        chart = next(d for k, d in events if k == "tool_end")["artifact"]
        assert chart["kind"] == "chart" and chart["title"] == "Revenue by category"
        done = events[-1][1]
        assert done["answer"] == "Furniture sells most."

        thread = client.get(f"/chat/threads/{done['thread_id']}").json()
        assert thread["exchanges"][0]["question"] == "Which category sells most?"
        assert thread["exchanges"][0]["steps"][0]["artifact"]["kind"] == "chart"
        assert client.get(f"/chat/threads/{'0' * 32}").status_code == 404
        assert client.post("/chat", json={"message": ""}).status_code == 422


def test_chat_explains_missing_setup_and_model_errors(tmp_path):
    class NotReady(ScriptedProvider):
        def ready(self):
            return False, "Set ANTHROPIC_API_KEY."

    with TestClient(create_app(Database(), tmp_path, provider=NotReady([]), checkpointer=InMemorySaver())) as client:
        events = sse_events(client.post("/chat", json={"message": "hi"}).text)
        assert events[-1] == ("error", {"message": "Set ANTHROPIC_API_KEY."})

    failing = ScriptedProvider([])  # raises ProviderError: no more turns
    with TestClient(create_app(Database(), tmp_path, provider=failing, checkpointer=InMemorySaver())) as client:
        events = sse_events(client.post("/chat", json={"message": "hi"}).text)
        assert events[-1][0] == "error" and "no more turns" in events[-1][1]["message"]
    assert issubclass(ProviderError, RuntimeError)


def test_step_summaries_keep_whole_lines():
    from app.agent.graph import SUMMARY_CHARS, summary

    long = "\n".join(f"- row {i}: " + "x" * 30 for i in range(40))
    short = summary(long)
    assert len(short) <= SUMMARY_CHARS + 2 and short.endswith("\n…")
    assert all(line.startswith("- row") or line == "…" for line in short.splitlines())
    assert summary("brief") == "brief"
