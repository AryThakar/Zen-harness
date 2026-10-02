"""generate_image: an image from a description, saved in the workspace and shown in the chat.

Ported from the Zen Rust image tool (native_tools/image_generation.rs). The image comes from
Pollinations (the Flux model; free, no key needed - set POLLINATIONS_TOKEN to lift the anonymous
rate limit of about one image per 15 seconds). Whatever comes back is checked to be a real PNG /
JPEG / WebP image before it is saved: an error page is never stored or shown as a picture.
The model gets the image's id and size, not the image.
"""

from __future__ import annotations

import io
import os
from typing import Literal
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError

from . import net
from .core import ToolError, ToolResult, ok, tool
from .workspace import Workspace

ENDPOINT = "https://image.pollinations.ai/prompt"
SIZES = {"square": (1024, 1024), "wide": (1344, 768), "tall": (768, 1344)}
MAX_PROMPT = 4000
MAX_BYTES = 25 * 1024 * 1024
FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}


@tool
def generate_image(ws: Workspace, prompt: str, size: Literal["square", "wide", "tall"] = "square", *,
                   http: httpx.Client | None = None) -> ToolResult:
    """Generate one image from a detailed visual description."""
    prompt = " ".join(str(prompt).split())
    if not prompt:
        raise ToolError("prompt is empty; describe the image.", field="prompt")
    if len(prompt) > MAX_PROMPT:
        raise ToolError(f"prompt is longer than {MAX_PROMPT} characters.", field="prompt")
    width, height = SIZES[size]
    endpoint = os.environ.get("POLLINATIONS_ENDPOINT", ENDPOINT).rstrip("/")
    token = os.environ.get("POLLINATIONS_TOKEN")
    http = http or net.client(timeout=90.0)
    try:
        r = net.get(http, f"{endpoint}/{quote(prompt, safe='')}", attempts=3,
                    params={"width": width, "height": height, "model": "flux", "nologo": "true", "private": "true"},
                    headers={"Authorization": f"Bearer {token}"} if token else None)
    except net.NetError as exc:
        raise ToolError(f"The image service failed: {exc.message}." + (" Try again in a minute." if exc.retryable else ""),
                        invalid=False) from exc
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
    ext, mime = FORMATS[fmt]
    image_id = ws.add_bytes("image", data, ext, name=prompt[:80],
                            meta={"prompt": prompt, "width": w, "height": h, "mime": mime, "provider": "pollinations/flux"})
    return ok({"image": image_id, "width": w, "height": h, "summary": "The image was generated and is shown to the user."},
              {"type": "image", "id": image_id, "mime": mime, "width": w, "height": h, "prompt": prompt})
