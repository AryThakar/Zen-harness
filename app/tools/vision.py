"""view_image: main looks at an image item and gets an answer to a question about it.

The model can't read pixels from a file; this asks Gemma's own vision encoder (the projector loaded in
llama-server, docs/TARGET_MAP.md section 8) one question about one image, with thinking off, and
returns its answer as text. It runs on the helpers' slot, idle while main works. Measured on a real
chart (air passengers, 3 lines): 3-4 s a question, about 140 image tokens; it named the lines and the
legend and read a peak as 'roughly 600' (622) - right for what a picture shows, not for exact values,
which is said with every answer. main keeps a text answer rather than the image: a chat runs for many
turns, and an image would sit in its window (and every request) for all of them.

A helper doesn't use it: a helper that sees images (the coder) is shown them in its own conversation
(workers/runner.py, _pictures) - 9 of 10 known-answer questions right there vs 8 of 10 here, with the task in
mind, and without replacing the slot's cached prompt.

Images are sent at most MAX_SIDE pixels on their longer side (PNG, or JPEG for photos), so a large
photo costs no more than a chart.
"""

from __future__ import annotations

import io
from collections.abc import Callable

from PIL import Image

from .core import ToolError, ToolResult, ok, tool
from .workspace import Workspace

MAX_SIDE = 1024
CAVEAT = ("What the vision model saw: right for what is shown and where, rough for exact numbers and small text - "
          "check exact values in the data or the code that drew it.")

Look = Callable[[bytes, str, str], str]  # (image bytes, mime type, question) -> answer


@tool
def view_image(ws: Workspace, image: str, question: str, look: Look | None) -> ToolResult:
    """Answer ``question`` about the image item ``image``."""
    if look is None:
        raise ToolError("Looking at images needs the local model's vision part, which isn't loaded here.",
                        invalid=False)
    question = " ".join(str(question or "").split())
    if not question:
        raise ToolError("Give question: what to look for, e.g. 'Does the title say 2025?' or 'Describe it'.",
                        field="question")
    entry = ws.entry(image)
    data, mime = prepare(ws.path(image).read_bytes())
    try:
        answer = look(data, mime, question).strip()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - the server's message is what the model can act on
        raise ToolError(f"The vision model couldn't look at {image}: {exc}", invalid=False) from exc
    meta = entry.get("meta", {})
    size = f"{meta['width']}x{meta['height']}" if meta.get("width") else None
    return ok({"image": image, "name": entry["name"], **({"size": size} if size else {}), "question": question,
               "answer": answer, "note": CAVEAT})


def prepare(data: bytes) -> tuple[bytes, str]:
    """The image as the vision model gets it: at most MAX_SIDE on its longer side, PNG or JPEG."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - any decoder error means the same to the model
        raise ToolError(f"This is not a readable image ({type(exc).__name__}).", invalid=False) from exc
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)  # an animated GIF: its first frame
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    photo = img.mode in ("RGB", "L") and img.format == "JPEG"
    img = img.convert("RGB") if photo or img.mode not in ("RGB", "RGBA", "L") else img
    out = io.BytesIO()
    img.save(out, format="JPEG" if photo else "PNG", **({"quality": 90} if photo else {}))
    return out.getvalue(), "image/jpeg" if photo else "image/png"
