"""Text embeddings for semantic search.

An Embedder turns passages and questions into L2-normalised float32 vectors, so
a dot product is the cosine similarity. Chunks remember which model embedded
them; after a model change they are re-embedded (Library.reindex).

* FastEmbedder - a small ONNX model run on the CPU by fastembed (default
  BAAI/bge-small-en-v1.5: 384 dimensions, ~65 MB, downloaded on first use).
  No GPU, no PyTorch, no service to run.
* Ollama (week 6) - nomic-embed-text from the local model server.
* HashEmbedder - word hashing, for tests: deterministic and instant, but only
  lexical, not semantic.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class EmbedderUnavailable(RuntimeError):
    """The model can't be loaded (e.g. offline on first use); search falls back to keywords."""


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


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

    def embed_documents(self, texts: list[str]) -> np.ndarray:
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

    def embed_documents(self, texts: list[str]) -> np.ndarray:
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
    return FastEmbedder(settings.embed_model, settings.embed_cache_dir)


def _normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / np.where(norms == 0, 1, norms)).astype(np.float32)
