"""generate_image: an image from a description, saved in the workspace and shown in the chat.

Ported from the Zen Rust image tool (native_tools/image_generation.rs). The image comes from
Pollinations' API (gen.pollinations.ai, the Z-Image Turbo model, 0.004 Pollen an image) and needs
the user's key: POLLINATIONS_KEY (or POLLINATIONS_TOKEN), an sk_ key from enter.pollinations.ai.
Pollen is the service's credit; new accounts earn free Pollen from one-time quests. Their old
keyless endpoint (image.pollinations.ai) now asks anonymous callers for a crypto micropayment
(HTTP 402, x402) - it is not used. Whatever comes back is checked to be a real PNG / JPEG / WebP
image before it is saved: an error page is never stored or shown as a picture. The model gets the
image's id and size, not the image - and, when the vision model is loaded, what the picture shows
(its own text quoted), so it can check the request was met before saying so: an image model often
misspells or leaves out requested text and gets counts wrong.
"""

from __future__ import annotations

import io
import os
import re
from contextlib import nullcontext
from typing import Literal
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError

from . import net
from .core import ToolError, ToolResult, ok, tool
from .filetypes import IMAGE_FORMATS as FORMATS
from .vision import prepare
from .workspace import Workspace

ENDPOINT = "https://gen.pollinations.ai/image"
# Z-Image Turbo: 8-11 s an image, measured (FLUX.1-schnell, half the price, took 43, 50 and 9 s).
# POLLINATIONS_MODEL picks another one.
MODEL = "zimage"
KEYS_PAGE = "https://enter.pollinations.ai/keys"
SIZES = {"square": (1024, 1024), "wide": (1344, 768), "tall": (768, 1344)}
MAX_PROMPT = 4000
NAME_CHARS = 40  # of the file name made from the prompt
MAX_BYTES = 25 * 1024 * 1024
SELF_CHECK = "Describe this image in one or two sentences. If it contains any writing, quote it exactly."
CHECK = ("Compare this with what the user asked for. If a requested detail (text, how many things, a colour) is "
         "missing or wrong, say so plainly - or generate it once more with a clearer prompt.")


@tool
def generate_image(ws: Workspace, prompt: str, size: Literal["square", "wide", "tall"] = "square", *,
                   http: httpx.Client | None = None, look=None) -> ToolResult:
    """Generate one image from a detailed visual description."""
    prompt = " ".join(str(prompt).split())
    if not prompt:
        raise ToolError("prompt is empty; describe the image.", field="prompt")
    if len(prompt) > MAX_PROMPT:
        raise ToolError(f"prompt is longer than {MAX_PROMPT} characters.", field="prompt")
    width, height = SIZES[size]
    endpoint = os.environ.get("POLLINATIONS_ENDPOINT", ENDPOINT).rstrip("/")
    key = os.environ.get("POLLINATIONS_KEY") or os.environ.get("POLLINATIONS_TOKEN")
    if not key:
        raise ToolError("Image generation is not set up: it needs a Pollinations key (free to get at "
                        f"{KEYS_PAGE}) in the POLLINATIONS_KEY setting. Tell the user.", invalid=False)
    url = f"{endpoint}/{quote(prompt, safe='')}"
    model = os.environ.get("POLLINATIONS_MODEL", MODEL)
    params = {"model": model, "width": width, "height": height, "seed": -1}  # -1: a new image every time
    try:
        with (net.client(timeout=90.0) if http is None else nullcontext(http)) as client:
            r = net.get(client, url, params=params, attempts=3, headers={"Authorization": f"Bearer {key}"})
    except net.NetError as exc:
        why = {401: f"the Pollinations key was refused - check POLLINATIONS_KEY ({KEYS_PAGE})",
               402: "the Pollinations account has no Pollen left - claim free Pollen or top up at "
                    "enter.pollinations.ai",
               403: "the Pollinations key is not allowed to use this model"}.get(exc.status, exc.message)
        raise ToolError(f"The image service failed: {why}." + (" Try again in a minute." if exc.retryable else "")
                        + (" Tell the user." if exc.status in (401, 402, 403) else ""), invalid=False) from exc
    data = r.content
    if len(data) > MAX_BYTES:
        raise ToolError("The image service sent a file larger than 25 MB.", invalid=False)
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt, (w, h) = img.format, img.size
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ToolError("The image service sent something that is not a valid image.", invalid=False) from exc
    if fmt not in FORMATS:
        raise ToolError(f"The image service sent an unsupported format ({fmt}).", invalid=False)
    mime = FORMATS[fmt][1]
    image_id = ws.add_image(data, file_name(prompt, FORMATS[fmt][0]),
                            meta={"prompt": prompt, "provider": f"pollinations/{model}"})
    result = {"image": image_id, "width": w, "height": h, "summary": "The image was generated and is shown to the user."}
    seen = _look(look, data)
    if seen:
        result.update(what_it_shows=seen, check=CHECK)
    return ok(result, {"type": "image", "id": image_id, "mime": mime, "width": w, "height": h, "prompt": prompt})


def file_name(prompt: str, ext: str) -> str:
    """A short file name from the prompt's first words, with its extension: 'a-teal-square.png'. Measured:
    named after the prompt itself, with no extension, a script couldn't open the picture by its name."""
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    stem = ""
    for word in words:
        if len(stem) + len(word) + 1 > NAME_CHARS:
            break
        stem = f"{stem}-{word}" if stem else word
    return f"{stem or 'image'}.{ext}"


def _look(look, data: bytes) -> str | None:
    """What the vision model sees in the new picture; None without vision or when it fails (the image stands)."""
    if look is None:
        return None
    try:
        return look(*prepare(data), SELF_CHECK).strip() or None
    except Exception:  # noqa: BLE001 - a failed look never costs the user the image
        return None
