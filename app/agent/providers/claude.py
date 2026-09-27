"""Claude through the official Anthropic Python SDK.

- Streams every turn (answers appear as they are written) with refusal fallbacks
  on: if Claude's safety classifiers decline, the API re-runs the request on
  Anthropic's recommended fallback model server-side (``fallbacks="default"``).
- Tool inputs stream eagerly, so the SDK hands back whatever it parsed; the
  graph validates every input against the tool's schema before running it, and
  a truncated (max_tokens) or refused turn never runs its tools.
- Assistant turns are replayed exactly as received (thinking blocks included) -
  history is append-only - except for what the API requires dropping: tool calls
  that never got results, and blocks before a mid-answer fallback.
- The system prompt and tools are cached between turns (``cache_control``).
"""

from __future__ import annotations

import os

import anthropic

from .base import EventCallback, ModelTurn, ProviderError

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 64_000  # streaming, so no HTTP-timeout concern; thinking counts against it
MAX_JSON_RETRIES = 2
MODEL_INTERNAL = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


class ClaudeProvider:
    name = "anthropic"

    def __init__(self, model: str, effort: str | None = None,
                 base_url: str = "https://api.anthropic.com", client: anthropic.Anthropic | None = None):
        self.model = model
        self.effort = effort
        self._base_url = base_url
        self._client = client

    def ready(self) -> tuple[bool, str]:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or self._client:
            return True, f"Claude ({self.model})"
        return False, "Set ANTHROPIC_API_KEY in the environment to use Claude, then restart the API."

    def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                 allow_tools: bool = True, on_event: EventCallback | None = None) -> ModelTurn:
        # No "thinking" field: Claude Opus 5 thinks adaptively by default (omitting it
        # equals {"type": "adaptive"}); effort, when set, controls how much.
        params = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": to_anthropic_messages(messages),
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            params["tools"] = [{**t, "eager_input_streaming": True} for t in tools]
            if not allow_tools:
                params["tool_choice"] = {"type": "none"}
        if self.effort:
            params["output_config"] = {"effort": self.effort}

        client = self._client or anthropic.Anthropic(base_url=self._base_url)
        for attempt in range(MAX_JSON_RETRIES + 1):
            try:
                with client.beta.messages.stream(**params) as stream:
                    for event in stream:
                        if event.type == "text" and on_event:
                            on_event("text", event.text)
                    message = stream.get_final_message()
                break
            except ValueError as exc:
                # A streamed tool input the SDK couldn't parse at all: there is no
                # tool_use id to answer, so re-issue the turn (bounded).
                if attempt == MAX_JSON_RETRIES:
                    raise ProviderError("The model produced an unreadable tool call; please try again.") from exc
                if on_event:
                    on_event("restart", "")
            except anthropic.AuthenticationError as exc:
                raise ProviderError("Claude rejected the API key - check ANTHROPIC_API_KEY.") from exc
            except anthropic.PermissionDeniedError as exc:
                raise ProviderError(f"This API key can't use {self.model}.") from exc
            except anthropic.NotFoundError as exc:
                raise ProviderError(f"Model '{self.model}' was not found - check BIZ_LLM_MODEL.") from exc
            except anthropic.RateLimitError as exc:
                raise ProviderError("Claude's rate limit was reached; wait a moment and try again.") from exc
            except anthropic.APIStatusError as exc:
                raise ProviderError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise ProviderError("Could not reach the Claude API; check the network.") from exc
        return parse_message(message)


def parse_message(message) -> ModelTurn:
    """Turn an SDK message into a ModelTurn. Refused or truncated turns get no
    tool calls: a refusal can cut a tool call off, and a max_tokens stop leaves a
    partial input that still parses."""
    content = [block.to_dict() for block in message.content]
    boundary = _last_fallback(content)
    after = content[boundary + 1:]
    text = "".join(b["text"] for b in content if b["type"] == "text")
    calls = [{"id": b["id"], "name": b["name"], "input": b["input"]} for b in after if b["type"] == "tool_use"]
    stop = {"tool_use": "tool_use", "refusal": "refusal", "max_tokens": "max_tokens"}.get(message.stop_reason, "end")
    if stop in ("refusal", "max_tokens"):
        calls = []
    if stop == "refusal" and not text:
        text = "I can't help with that request."
    return ModelTurn(text=text, tool_calls=calls, raw=content, stop=stop, model=message.model)


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Neutral history -> Messages API shape. All tool results that answer one
    assistant turn go back in a single user message."""
    out: list[dict] = []
    for i, msg in enumerate(messages):
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            answered = _answered_ids(messages, i)
            blocks = _assistant_blocks(msg, answered)
            if any(b["type"] in ("text", "tool_use") for b in blocks):
                out.append({"role": "assistant", "content": blocks})
            # else: nothing replayable (e.g. an empty refusal) - skip the turn;
            # consecutive user messages are merged by the API.
        elif role == "tool":
            result = {"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}
            if msg.get("is_error"):
                result["is_error"] = True
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def _assistant_blocks(msg: dict, answered: set[str]) -> list[dict]:
    raw = msg.get("raw") if msg.get("provider") == ClaudeProvider.name else None
    if raw is None:  # written by another provider: rebuild plain blocks
        blocks = [{"type": "text", "text": msg["text"]}] if msg.get("text") else []
        blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                   for c in msg.get("tool_calls", []) if c["id"] in answered]
        return blocks
    boundary = _last_fallback(raw)
    blocks = []
    for index, block in enumerate(raw):
        kind = block["type"]
        if kind == "fallback":
            continue  # audit marker only
        if index < boundary and kind in MODEL_INTERNAL:
            continue  # the model that wrote these declined; only its text carries over
        if kind == "tool_use" and block["id"] not in answered:
            continue  # a call that never ran (refused, truncated, round limit)
        blocks.append(block)
    return blocks


def _answered_ids(messages: list[dict], index: int) -> set[str]:
    ids = set()
    for msg in messages[index + 1:]:
        if msg["role"] != "tool":
            break
        ids.add(msg["tool_call_id"])
    return ids


def _last_fallback(content: list[dict]) -> int:
    positions = [i for i, b in enumerate(content) if b["type"] == "fallback"]
    return positions[-1] if positions else -1
