"""Ask AI: chat with the assistant about the data. Answers stream in; the tools it
used (queries, calculations, models) are listed above each answer, and its
charts are drawn in house style."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import charts
import client

SUGGESTIONS = [
    "How did revenue change over the last 3 months?",
    "Which 5 products bring in the most profit?",
    "Forecast revenue for the next 30 days",
    "Which customers are we about to lose?",
    "Were there any unusual days recently?",
]

STEP_LABELS = {
    "describe_data": "Looked at how the data is organised",
    "run_sql": "Queried the data",
    "business_summary": "Calculated the headline figures",
    "calculate": "Did a calculation",
    "business_metric": "Worked out a business metric",
    "make_chart": "Drew a chart",
    "forecast": "Ran the forecast model",
    "customer_segments": "Grouped customers by buying behaviour",
    "unusual_days": "Looked for unusual days",
}

PLOT_CONFIG = {"displayModeBar": False}


def render() -> None:
    header, reset = st.columns([5, 1], vertical_alignment="bottom")
    header.header("Ask AI")
    if reset.button("New conversation", key="chat_reset", width="stretch"):
        st.session_state.pop("chat_thread", None)
        st.rerun()

    try:
        status = client.chat_status()
        thread_id = st.session_state.get("chat_thread")
        history = client.chat_thread(thread_id) if thread_id else []
    except client.ApiError as exc:
        st.error(str(exc))
        return

    if status["ready"]:
        st.caption(f"Answers are worked out from your data by {status['model']}: every number comes from a query, "
                   "a calculation or a model, and each step is listed above the answer.")
    else:
        _setup_help(status)

    for i, exchange in enumerate(history):
        _exchange(exchange, key=f"h{i}")

    question = st.chat_input("Ask about your sales…", disabled=not status["ready"], key="chat_input")
    if not history and not question and status["ready"]:
        question = _suggestions()
    if question:
        _ask(question, thread_id)


def _setup_help(status: dict) -> None:
    st.warning(status["message"])
    with st.expander("How to set up the assistant", expanded=True):
        st.markdown(
            "The assistant runs on Claude through the Anthropic API for now (a local model is planned).\n"
            "1. Create an API key at console.anthropic.com.\n"
            "2. Start the API with the key in its environment:\n"
            "   ```bash\n   export ANTHROPIC_API_KEY=...\n   uvicorn app.main:app\n   ```\n"
            "3. Optional: choose the model with `BIZ_LLM_MODEL` (default `claude-opus-5`)."
        )


def _suggestions() -> str | None:
    slot = st.empty()
    with slot.container():
        st.markdown("**Try asking**")
        with st.container(horizontal=True):  # wraps onto more rows on narrow screens
            clicked = [st.button(text, key=f"chat_suggestion_{i}") for i, text in enumerate(SUGGESTIONS)]
        picked = next((text for text, hit in zip(SUGGESTIONS, clicked) if hit), None)
    if picked:
        slot.empty()  # the conversation takes their place
    return picked


# --- one exchange ------------------------------------------------------------

def _exchange(exchange: dict, key: str) -> None:
    """A finished question and answer, from the saved conversation."""
    with st.chat_message("user"):
        st.markdown(_md(exchange["question"]))
    with st.chat_message("assistant"):
        steps = exchange["steps"]
        if steps:
            box = st.status(_steps_label(steps), state="complete", expanded=False)
            for step in steps:
                _step(box, step, key)
        for step in steps:
            _artifact(step, key)
        if exchange["answer"]:
            st.markdown(_md(exchange["answer"]))
        elif not steps:
            st.caption("No answer - the request failed.")


def _ask(question: str, thread_id: str | None) -> None:
    """Send a question and draw the answer as it streams in."""
    with st.chat_message("user"):
        st.markdown(_md(question))
    with st.chat_message("assistant"):
        steps_slot, artifacts_slot, answer_slot = st.container(), st.container(), st.empty()
        box, steps = None, {}
        text, turn_start = "", 0  # turn_start: where the current model turn's text begins
        with answer_slot:
            st.caption("Thinking…")
        try:
            for event, data in client.chat(question, thread_id):
                if event == "thread":
                    st.session_state["chat_thread"] = data["thread_id"]
                elif event == "text":
                    text += data["text"]
                    answer_slot.markdown(_md(text) + " ▌")
                elif event == "restart":  # the model restarts this turn; drop what it streamed
                    text = text[:turn_start]
                    answer_slot.markdown(_md(text) if text else "")
                elif event == "tool_start":
                    if text[turn_start:].strip():
                        text = text.rstrip() + "\n\n"
                    turn_start = len(text)
                    if box is None:
                        box = steps_slot.status("Working…", state="running", expanded=False)
                    steps[data["id"]] = dict(data)
                    box.update(label=f"{STEP_LABELS.get(data['name'], data['name'])}…")
                elif event == "tool_end":
                    step = steps.setdefault(data["id"], {"input": {}}) | data
                    steps[data["id"]] = step
                    if box is None:
                        box = steps_slot.status("Working…", state="running", expanded=False)
                    _step(box, step, "live")
                    with artifacts_slot:
                        _artifact(step, "live")
                elif event == "done":
                    answer_slot.markdown(_md(data["answer"]) if data["answer"] else "")
                elif event == "error":
                    answer_slot.markdown(_md(text) if text.strip() else "")
                    st.error(data["message"])
        except client.ApiError as exc:
            st.error(str(exc))
        if box is not None:
            box.update(label=_steps_label(list(steps.values())), state="complete")


def _steps_label(steps: list[dict]) -> str:
    failed = sum(1 for s in steps if s.get("is_error"))
    label = f"{len(steps)} step{'s' if len(steps) != 1 else ''}"
    return label + (f" ({failed} retried)" if failed else "")


def _step(box, step: dict, key: str) -> None:
    """One tool step inside the steps box: what was done, with what, and the result."""
    box.markdown(f"**{STEP_LABELS.get(step['name'], step['name'])}**")
    inputs = step.get("input") or {}
    if "sql" in inputs:
        box.code(inputs["sql"], language="sql")
    elif "expression" in inputs:
        box.code(inputs["expression"], language="text")
    elif inputs:
        box.caption(", ".join(f"{k}: {v}" for k, v in inputs.items()))
    artifact = step.get("artifact")
    if step.get("is_error"):
        box.caption(f"Didn't work, so the assistant adjusted: {step.get('summary', '')}")
    elif artifact and artifact["kind"] == "table":
        box.dataframe(pd.DataFrame(artifact["rows"], columns=artifact["columns"]), hide_index=True,
                      width="stretch", height=min(35 * (len(artifact["rows"]) + 1) + 3, 300),
                      key=f"chat_table_{key}_{step['id']}")
        if artifact["total_rows"] > len(artifact["rows"]):
            box.caption(f"First {len(artifact['rows'])} of {artifact['total_rows']:,} rows.")
    elif step.get("summary") and step["name"] not in ("make_chart",):
        box.caption(_md(step["summary"]).replace("\n", "  \n"))


def _artifact(step: dict, key: str) -> None:
    """Charts from a step, drawn next to the answer (tables stay in the steps box)."""
    a = step.get("artifact")
    if not a or step.get("is_error") or a["kind"] == "table":
        return
    element_key = f"chat_chart_{key}_{step['id']}"
    st.markdown(f"**{a['title']}**")
    if a["kind"] == "chart":
        fmt = charts.formatter(a.get("unit", "number"))
        if a["chart_type"] == "line":
            fig = charts.line_figure(a["points"], fmt, x_title=a["x"])
        else:
            items = [{"label": str(p["x"]), "value": p["y"]} for p in a["points"]]
            fig = charts.bar_figure(items, value_key="value", sort=False, fmt=fmt)
    elif a["kind"] == "forecast":
        fig = charts.forecast_figure(a["history"], a["points"], a["grain"])
    elif a["kind"] == "anomalies":
        fig = charts.anomaly_figure(a["series"], a["days"], "revenue")
    else:
        return
    st.plotly_chart(fig, width="stretch", config=PLOT_CONFIG, key=element_key)


def _md(text: str) -> str:
    """Markdown-safe text: a dollar sign would otherwise start a LaTeX formula."""
    return text.replace("$", "\\$")
