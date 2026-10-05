"""Trace the main slot end to end: a running server, real models on both slots, every streamed event
in order with the second it arrived - what main did itself, what it handed to a worker, what the
worker did, what was shown, the questions and the answers.

    uvicorn app.main:app --port 8000            (with llama-server running)
    python -m bench.main.trace [http://127.0.0.1:8000]      -> bench/main/traces.md

It talks HTTP like the UI does (a test client would buffer the stream), and deletes the workspaces
it made. Conversations: a direct tool, an upload and a data question, a follow-up in the same chat,
a new chat asking about the first one (recall across chats), and a question only the user can answer.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

from bench.workers.tasks import DATA

HERE = Path(__file__).resolve().parent


def converse(client: httpx.Client, url: str, body: dict, lines: list[str]) -> dict:
    """Send one message (or answer); log its events; return the question asked or the answer."""
    started, text, last, kind = time.perf_counter(), "", {}, None
    with client.stream("POST", url, json=body) as response:
        for raw in response.iter_lines():
            if raw.startswith("event: "):
                kind = raw[7:]
                continue
            if not raw.startswith("data: "):
                continue
            data, t = json.loads(raw[6:]), time.perf_counter() - started
            if kind == "text":
                if not text:
                    lines.append(f"| {t:.1f} | main starts writing | |")
                text += data["text"]
                continue
            if text.strip():
                lines.append(f"| {t:.1f} | main wrote | {_cell(text.strip()[:240])} |")
                text = ""
            if kind == "tool_start":
                lines.append(f"| {t:.1f} | main calls | **{data['name']}** {_cell(json.dumps(data['input'])[:200])} |")
            elif kind == "tool_end":
                lines.append(f"| {t:.1f} | result | {data['name']}: {data['status']} - {_cell(data['summary'][:200])} |")
            elif kind == "worker":
                what = (data.get("name") or ", ".join(data.get("calls", [])) or data.get("text", "")[:80]
                        or ("; ".join(data["problems"]) + (" -> retry" if data.get("retry") else "")
                            if data.get("problems") else ("passed" if data["event"] == "check" else "")))
                extra = data.get("summary", "")[:120] if data["event"] == "result" else ""
                lines.append(f"| {t:.1f} | {data['worker']} worker: {data['event']} | {_cell(str(what))} {_cell(extra)} |")
            elif kind == "artifact":
                a = data["artifact"]
                lines.append(f"| {t:.1f} | shown to user | {a.get('type')} from {a.get('tool')} |")
            elif kind == "question":
                lines.append(f"| {t:.1f} | **question** | {_cell(data['question'])} options: {data.get('options')} |")
                last = {"question": data}
            elif kind == "done":
                lines.append(f"| {t:.1f} | **answer** | {_cell(data['answer'][:400])} |")
                last = {"answer": data["answer"]}
            elif kind == "error":
                lines.append(f"| {t:.1f} | **error** | {_cell(data['message'])} |")
                last = {"error": data["message"]}
    return last


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def section(title: str) -> list[str]:
    return [f"## {title}", "", "| s | event | what happened |", "|---|---|---|"]


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/") + "/assistant"
    out = ["# Main slot traces", "", "A running server with real models on both slots (Gemma 4 E4B, thinking "
           "on), over HTTP like the UI. Each row is one streamed event with the second it arrived.", ""]
    made = []
    with httpx.Client(base_url=base, timeout=600) as c:
        try:
            ws = c.post("/workspaces", json={"name": "Pizza shop (trace)"}).json()["id"]
            made.append(ws)
            c.post(f"/workspaces/{ws}/files",
                   files={"file": ("pizza_sales.csv", (DATA / "pizza_sales.csv").read_bytes(), "text/csv")})
            first = c.post(f"/workspaces/{ws}/chats").json()["id"]
            for text in ["What is 15% of 2400?",
                         "I uploaded our pizza sales. What was the total revenue (quantity times price)?",
                         "And which pizza category earned the most?"]:
                out += section(text)
                converse(c, f"/chats/{first}/messages", {"text": text}, out)
                out.append("")
            second = c.post(f"/workspaces/{ws}/chats").json()["id"]
            text = "What did we find out about revenue in our earlier chat?"
            out += section(f"New chat: {text}")
            converse(c, f"/chats/{second}/messages", {"text": text}, out)
            out.append("")
            rows = ["date,amount"] + [f"{d:02d}/{m:02d}/2025,{100 + d}" for m in (1, 2, 3) for d in (1, 3, 5, 7, 9, 11)]
            ws2 = c.post("/workspaces", json={"name": "Q1 sales (trace)"}).json()["id"]
            made.append(ws2)
            c.post(f"/workspaces/{ws2}/files", files={"file": ("sales_q1.csv", "\n".join(rows).encode(), "text/csv")})
            third = c.post(f"/workspaces/{ws2}/chats").json()["id"]
            text = "What were the total sales in February 2025?"
            out += section(f"A question only the user can answer: {text}")
            last = converse(c, f"/chats/{third}/messages", {"text": text}, out)
            if "question" in last:
                options = last["question"].get("options") or []
                choice = next((o for o in options if "day" in o.lower()), "Day first")
                out.append(f"| | **user answers** | {choice} |")
                converse(c, f"/chats/{third}/answer", {"answer": choice}, out)
            out.append("")
        finally:
            for ws_id in made:
                c.delete(f"/workspaces/{ws_id}")
    (HERE / "traces.md").write_text("\n".join(out), encoding="utf-8")
    print("-> bench/main/traces.md")


if __name__ == "__main__":
    main()
