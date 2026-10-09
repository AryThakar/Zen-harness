"""Does a llama-server slot leak one conversation into another? Two conversations, each holding its own code word,
asked one after the other on one slot, then both at once on one slot (each request straight to the server), and
each answer checked for the other's word.

Asked after a run on the 26B (Kaggle, b11474, two slots) where a coder, given a task about dates, asked for an
'invoices.csv' that another job was using. Measured 2026-10-09: 0 of 10 leaked one after the other, 0 of 10 at once
on one slot, 0 of 10 at once on the two slots - no leak. llama-server queues a request for a busy slot itself, so
the app needs no lock of its own; the coder had made the name up (a common one), a model slip to measure.

    python -m bench.slot_leak [rounds]        (llama-server at BIZ_LLAMACPP_URL)
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

sys.path.insert(0, ".")
from app.config import settings  # noqa: E402

WORDS = ("PINEAPPLE-42", "GLACIER-77")
FILLER = " ".join(f"Line {i}: the warehouse inventory note for aisle {i} is unchanged." for i in range(120))


def ask(word: str, slot: int) -> str:
    """A conversation that holds its code word deep in a long prompt, then asks which word it holds."""
    messages = [{"role": "system", "content": "You answer in one line."},
                {"role": "user", "content": f"{FILLER}\nThe code word for this conversation is {word}.\n{FILLER}\n"
                                            "Which code word was given in this conversation? Answer with the word only."}]
    body = {"model": settings.llamacpp_model, "messages": messages, "max_tokens": 40, "id_slot": slot,
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}
    request = urllib.request.Request(settings.llamacpp_url.rstrip("/") + "/v1/chat/completions",
                                     json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)["choices"][0]["message"].get("content") or ""


def leaked(word: str, answer: str) -> bool:
    other = WORDS[1] if word == WORDS[0] else WORDS[0]
    return other in answer


def main(rounds: int) -> None:
    sequential = 0
    for _ in range(rounds):
        for word in WORDS:  # A, then B on the same slot: B's prompt reuses A's cache up to their common start
            sequential += leaked(word, ask(word, 1))
    together = 0
    for _ in range(rounds):
        answers: dict[str, str] = {}
        threads = [threading.Thread(target=lambda w=w: answers.__setitem__(w, ask(w, 1))) for w in WORDS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        together += sum(leaked(w, a) for w, a in answers.items())
    print(f"one after the other: {sequential} of {2 * rounds} answers held the other conversation's word")
    print(f"both at once:        {together} of {2 * rounds} answers held the other conversation's word")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
