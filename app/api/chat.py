"""Chat with the AI assistant.

POST /chat streams server-sent events while the assistant works:
    thread      {"thread_id"}                      first, so the client can continue the conversation
    text        {"text"}                           answer text as it is written
    restart     {}                                 discard text streamed so far for this model turn
    tool_start  {"id", "name", "input"}            a tool is running
    tool_end    {"id", "name", "is_error", "summary", "artifact"}
    stats       {"output_tokens", "prompt_tokens", "tokens_per_second", "seconds"}  after each model turn
    done        {"thread_id", "answer"}
    error       {"message"}
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.graph import new_question, summary
from app.agent.providers import ProviderError

router = APIRouter(prefix="/chat", tags=["chat"])
log = logging.getLogger(__name__)

THREAD_ID = r"^[0-9a-f]{32}$"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(None, pattern=THREAD_ID)


class ChatStatusOut(BaseModel):
    provider: str
    model: str
    ready: bool
    message: str


@router.get("/status", response_model=ChatStatusOut)
def chat_status(request: Request) -> ChatStatusOut:
    """Which model answers, and whether it is set up."""
    provider = request.app.state.provider
    ready, message = provider.ready()
    return ChatStatusOut(provider=provider.name, model=provider.model, ready=ready, message=message)


@router.post("")
def chat(body: ChatIn, request: Request) -> StreamingResponse:
    """Ask a question; the answer streams back as server-sent events."""
    app = request.app
    thread_id = body.thread_id or uuid.uuid4().hex

    def events():
        yield _sse("thread", {"thread_id": thread_id})
        ready, message = app.state.provider.ready()
        if not ready:
            yield _sse("error", {"message": message})
            return
        config = {"configurable": {"thread_id": thread_id}}
        try:
            for event in app.state.chat_graph.stream(new_question(body.message), config, stream_mode="custom"):
                yield _sse(event["type"], event)
        except ProviderError as exc:
            yield _sse("error", {"message": str(exc)})
            return
        except Exception:  # a bug, not the user's fault; details go to the log
            log.exception("Chat failed")
            yield _sse("error", {"message": "Something went wrong while answering. Please try again."})
            return
        messages = app.state.chat_graph.get_state(config).values["messages"]
        yield _sse("done", {"thread_id": thread_id, "answer": _answer(_current_exchange(messages))})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, request: Request) -> dict:
    """A conversation as question / steps / answer exchanges, for redisplay."""
    if not _valid_thread_id(thread_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid thread id.")
    state = request.app.state.chat_graph.get_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages") if state.values else None
    if not messages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    return {"thread_id": thread_id, "exchanges": exchanges(messages)}


def exchanges(messages: list[dict]) -> list[dict]:
    """Group the neutral history into exchanges: question, tool steps, answer."""
    result: list[dict] = []
    for msg in messages:
        if msg["role"] == "user":
            result.append({"question": msg["content"], "steps": [], "texts": []})
        elif not result:
            continue
        elif msg["role"] == "assistant":
            if msg.get("text"):
                result[-1]["texts"].append(msg["text"])
            for call in msg.get("tool_calls", []):
                result[-1]["steps"].append({"id": call["id"], "name": call["name"], "input": call["input"]})
        elif msg["role"] == "tool":
            for step in result[-1]["steps"]:
                if step["id"] == msg["tool_call_id"]:
                    step.update(is_error=msg["is_error"], summary=summary(msg["content"]), artifact=msg.get("artifact"))
    return [{"question": e["question"], "steps": e["steps"], "answer": _answer(e)} for e in result]


def _current_exchange(messages: list[dict]) -> dict:
    return exchanges(messages)[-1]


def _answer(exchange: dict) -> str:
    if "answer" in exchange:
        return exchange["answer"]
    return "\n\n".join(t.strip() for t in exchange["texts"] if t.strip())


def _sse(event: str, data: dict) -> str:
    payload = {k: v for k, v in data.items() if k != "type"}
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


def _valid_thread_id(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)
