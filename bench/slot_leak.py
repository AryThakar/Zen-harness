"""Does llama-server mix one conversation's output into another's? Two conversations, each with its own code word
and topic, asked to write long answers - one after the other on one slot, both at once on one slot, and both at
once on the two slots - and each answer checked for the other's word or topic.

Asked after a run on the 26B (Kaggle, b11474, two slots, MTP draft) where a coder's script held, mid-line, another
job's thinking about churn and a raw '<|tool_call>call:inspect_data' (a tool the coder doesn't have). A first
version of this check asked for one word (40 tokens, thinking off) and found nothing: the two generations barely
overlapped. This one makes both write for a while, thinking on, so their generations run side by side.

Measured 2026-10-09 on the 26B: 0 of 6 in each of the three ways (and 0 of 10 each with the one-word version).
The app itself never sends two requests at once - one turn at a time (app/assistant/main.py, _busy), helpers
inside it - so only two benchmark processes run side by side can, and the benchmarks are run one at a time.

    python -m bench.slot_leak [rounds] [--no-think]        (llama-server at BIZ_LLAMACPP_URL)
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request

sys.path.insert(0, ".")
from app.config import settings  # noqa: E402

TALKS = {"PINEAPPLE-42": ("tropical fruit farming", ("pineapple", "mango", "banana", "fruit")),
         "GLACIER-77": ("glaciers and polar ice", ("glacier", "iceberg", "polar", "arctic"))}


def ask(word: str, slot: int, think: bool) -> str:
    topic = TALKS[word][0]
    messages = [{"role": "system", "content": "You are a careful writer."},
                {"role": "user", "content": f"Write 30 numbered lines about {topic}. Start every line with the code "
                                            f"word {word}, e.g. '{word} 1: ...'. One sentence per line."}]
    body = {"model": settings.llamacpp_model, "messages": messages, "max_tokens": 2500, "id_slot": slot,
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": think}}
    request = urllib.request.Request(settings.llamacpp_url.rstrip("/") + "/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=900) as response:
        message = json.load(response)["choices"][0]["message"]
    return (message.get("reasoning_content") or "") + "\n" + (message.get("content") or "")


def leaked(word: str, answer: str) -> list[str]:
    """What of the other conversation is in this answer: its code word, or its topic's words."""
    other = next(w for w in TALKS if w != word)
    return [s for s in (other, *TALKS[other][1]) if re.search(rf"\b{re.escape(s)}", answer, re.I)]


def together(slots: tuple[int, int], think: bool) -> list[tuple[str, list[str]]]:
    answers: dict[str, str] = {}
    threads = [threading.Thread(target=lambda w=w, s=s: answers.__setitem__(w, ask(w, s, think)))
               for w, s in zip(TALKS, slots)]
    for t in threads:
        t.start()
        time.sleep(1.0)  # the second starts while the first is generating
    for t in threads:
        t.join()
    return [(w, leaked(w, a)) for w, a in answers.items()]


def main(rounds: int, think: bool) -> None:
    modes = {"one after the other, slot 1": lambda: [(w, leaked(w, ask(w, 1, think))) for w in TALKS],
             "both at once, slot 1": lambda: together((1, 1), think),
             "both at once, slots 0 and 1": lambda: together((0, 1), think)}
    for mode, run in modes.items():
        bad, seen = 0, []
        for _ in range(rounds):
            for word, found in run():
                if found:
                    bad += 1
                    seen.append(f"{word}: {found}")
        print(f"{mode:30} {bad} of {2 * rounds} answers held the other conversation" + (f"  {seen}" if seen else ""),
              flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(int(args[0]) if args else 3, think="--no-think" not in sys.argv)
