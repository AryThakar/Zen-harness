"""Embeddings for semantic search.

An Embedder turns passages and questions into L2-normalised float32 vectors, so
a dot product is the cosine similarity. Passages remember which model embedded
them (search_index.py): search only compares vectors of the current model.

* ServerEmbedder - a model served by llama-server (--embeddings): EmbeddingGemma 2,
  768 dimensions, text and - with its projector (--mmproj) - pictures in the same
  space, so a typed question can find a picture. What is the model's own - its
  prompts for a query and a document (its model card) and the similarity below
  which a match is no match (measured) - is in MODELS.
* FastEmbedder - a small ONNX model run on the CPU by fastembed (default
  BAAI/bge-small-en-v1.5: 384 dimensions, ~65 MB, downloaded on first use).
  No GPU, no PyTorch, no service to run. Text only. The fallback when no
  embedding server is set up.
* HashEmbedder - word hashing, for tests: deterministic and instant, but only
  lexical, not semantic.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class EmbedderUnavailable(RuntimeError):
    """The model can't be loaded (e.g. offline on first use); search falls back to keywords."""


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str], titles: list[str | None] | None = None) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


# What belongs to a model, by a word of its name. Prompts from the model card (a passage without a title
# gets "none"). The floors are cosine similarities measured on held-out questions about the user's own
# workspaces (bench/search_models.py, 2026-10-09): text - right passages scored 0.712 or more, the best
# passage for a question about nothing in the workspace 0.631 at most, cut-off chosen on half the questions;
# pictures - 15 captions scored 0.623-0.81 against their own picture, 20 unrelated questions 0.604 at most
# against any picture. unsure_gap (analyze_text's labels): EmbeddingGemma 2's gaps between the best and the
# second label were about half of bge-small's on the same texts (median 0.015 vs 0.0315), so half its gap.
MODELS = {
    "embeddinggemma": {"query": "task: search result | query: {text}", "document": "title: {title} | text: {text}",
                       "min_similarity": 0.68, "min_similarity_pictures": 0.64, "unsure_gap": 0.01},
}
MAX_CHARS = 16000  # per text: EmbeddingGemma reads 8,192 tokens; a passage is ~250 words
BATCH = 16
RETRY_AFTER = 30  # seconds before an unreachable server is tried again (it may have been started since)


class ServerEmbedder:
    """A model served by llama-server with --embeddings. Unreachable or failing, it raises
    EmbedderUnavailable and search falls back to keywords; it is tried again after RETRY_AFTER
    seconds, so a server started later is picked up without restarting the app. ``start`` (optional,
    embed_server.EmbedServer.start) starts the server when the app runs it: called when a request finds
    nothing listening, then the request is tried once more."""

    def __init__(self, url: str, name: str, timeout: float = 120, start=None):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        model = next((v for k, v in MODELS.items() if k in name.lower()), {})
        self._query, self._document = model.get("query", "{text}"), model.get("document", "{text}")
        if "min_similarity" in model:  # else search_index's default
            self.min_similarity = model["min_similarity"]
            self.min_similarity_pictures = model["min_similarity_pictures"]
            self.unsure_gap = model["unsure_gap"]
        self._start = start
        self._failed_at = 0.0
        self._marker: str | None = None

    @property
    def images(self) -> bool:
        """Whether the served model takes pictures (its projector is loaded)."""
        try:
            return self._media_marker() is not None
        except EmbedderUnavailable:
            return False

    def embed_documents(self, texts: list[str], titles: list[str | None] | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        titles = titles or [None] * len(texts)
        prompts = [self._document.format(title=" ".join((t or "none").split())[:200], text=x[:MAX_CHARS])
                   for x, t in zip(texts, titles)]
        return np.vstack([self._embed(prompts[i:i + BATCH]) for i in range(0, len(prompts), BATCH)])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self._query.format(text=text[:MAX_CHARS])])[0]

    def embed_images(self, images: list[bytes]) -> np.ndarray:
        """Vectors of pictures (PNG, JPEG, ...) in the same space as text - with no prompt, as the
        model card says. One request a picture: one media item per marker."""
        marker = self._media_marker()
        if marker is None:
            raise EmbedderUnavailable(f"The embedding server ({self.name}) has no picture projector loaded.")
        return np.vstack([self._embed([{"prompt_string": marker,
                                        "multimodal_data": [base64.b64encode(b).decode("ascii")]}]) for b in images])

    def _embed(self, content: list) -> np.ndarray:
        out = self._post("/embedding", {"content": content})
        try:
            rows = sorted(out, key=lambda r: r["index"])
            vectors = np.array([np.asarray(r["embedding"], dtype=np.float32).reshape(-1) for r in rows])
        except (TypeError, KeyError, ValueError) as exc:
            raise EmbedderUnavailable(f"The embedding server ({self.name}) sent an answer that could not be read.") from exc
        if len(vectors) != len(content):
            raise EmbedderUnavailable(f"The embedding server ({self.name}) sent {len(vectors)} vectors for "
                                      f"{len(content)} inputs.")
        return _normalise(vectors)

    def _media_marker(self) -> str | None:
        if self._marker is None:
            props = self._call(urllib.request.Request(self.url + "/props"))
            vision = (props.get("modalities") or {}).get("vision") if isinstance(props, dict) else None
            self._marker = (props.get("media_marker") or "") if vision else ""
        return self._marker or None

    def _post(self, path: str, body: dict):
        return self._call(urllib.request.Request(self.url + path, json.dumps(body).encode("utf-8"),
                                                 {"Content-Type": "application/json"}))

    def _call(self, req: urllib.request.Request):
        if self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER:
            raise EmbedderUnavailable(f"The embedding server ({self.name}) is not reachable at {self.url}.")
        try:
            try:
                answer = self._send(req)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, ConnectionError) as exc:
                if self._start is None or not _refused(exc):
                    raise
                self._start()  # nothing listening: start the server, then once more
                self._marker = None
                answer = self._send(req)
        except urllib.error.HTTPError as exc:  # the server answered: this input was refused, the server is fine
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise EmbedderUnavailable(f"The embedding server ({self.name}) refused the request: {detail}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, json.JSONDecodeError, RuntimeError) as exc:
            self._failed_at = time.monotonic()
            self._marker = None  # a server started again may have another marker
            log.warning("Embedding server %s unavailable: %s", self.url, exc)
            raise EmbedderUnavailable(f"The embedding server ({self.name}) is not available at {self.url}: {exc}") from exc
        self._failed_at = 0.0
        return answer

    def _send(self, req: urllib.request.Request):
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)


def _refused(exc: Exception) -> bool:
    """Nothing listening at the address (as opposed to a server that answered badly or too slowly)."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower()


class FastEmbedder:
    """fastembed model, loaded lazily (the first upload or search pays the load)."""

    def __init__(self, model: str, cache_dir: str | None = None):
        self.name = model
        self._cache_dir = cache_dir
        self._model = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                if self._error:
                    raise EmbedderUnavailable(self._error)
                try:
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(self.name, cache_dir=self._cache_dir)
                except Exception as exc:  # download or runtime failure: remember, don't retry every call
                    log.warning("Embedding model %s unavailable: %s", self.name, exc)
                    self._error = f"The embedding model {self.name} couldn't be loaded ({exc})."
                    raise EmbedderUnavailable(self._error) from exc
            return self._model

    def embed_documents(self, texts: list[str], titles: list[str | None] | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = np.array(list(self._load().embed(texts, batch_size=64)), dtype=np.float32)
        return _normalise(vectors)

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(np.array(list(self._load().query_embed([text])), dtype=np.float32))[0]


class HashEmbedder:
    """Bag of hashed words (and word pairs). Test double: shares words, not meaning."""

    def __init__(self, dim: int = 256):
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed_documents(self, texts: list[str], titles: list[str | None] | None = None) -> np.ndarray:
        return _normalise(np.array([self._vector(t) for t in texts], dtype=np.float32).reshape(len(texts), self.dim))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(self._vector(text)[None, :])[0]

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        words = re.findall(r"\w+", text.lower())
        for token in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
            v[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1
        return v


def make_embedder(settings) -> Embedder:
    """The embedding server when one is set up (ASSISTANT_EMBED_GGUF: the model file; the app starts
    llama-server itself when ASSISTANT_EMBED_SERVER names it), else fastembed's small CPU model."""
    if settings.embed_gguf:
        start = None
        if settings.embed_server:
            from .embed_server import EmbedServer
            start = EmbedServer(settings.embed_server, settings.embed_gguf, settings.embed_mmproj,
                                settings.embed_url, Path(settings.assistant_dir) / "embed-server.log").start
        return ServerEmbedder(settings.embed_url, Path(settings.embed_gguf).stem, start=start)
    return FastEmbedder(settings.embed_model, settings.embed_cache_dir)


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / np.where(norms == 0, 1, norms)).astype(np.float32)
