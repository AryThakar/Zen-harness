# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The model in use, as Zen's Model settings say now (app/model_link.py): main's and the helpers' providers, and the
search model - made again when the settings file changes (its time stamp, looked at on each use), so a change from
the app applies on the next request, with no restart.

One rule for search by meaning: it runs on the chat model's machine.

    Kaggle    the notebook's EmbeddingGemma (at its address's /embed, with its password) - this computer runs nothing
    This PC   EmbeddingGemma on this computer, set up from Settings (app/search_model.py; or ASSISTANT_EMBED_*)
    Cloud     the same, on this computer: a cloud's own embeddings would be another model's vectors, and every switch
              would vectorise every file again

Everywhere the same model file, so the vectors are one model's: a switch vectorises nothing again. Where it isn't (the
notebook stopped or older, nothing set up here), search is by keywords - nothing is probed or put in its place; a
search model that answers again is used again (app/rag/embed.py tries it after a pause).

The app holds stand-ins (Live, LiveEmbedder) that pass every use on to what is current.
"""

from __future__ import annotations

import threading
from pathlib import Path

from app import model_link, search_model
from app.providers import LlamaCppProvider
from app.providers.cloud import CloudProvider
from app.rag.embed import NoEmbedder, ServerEmbedder, local_embedder, make_embedder


class Connection:
    def __init__(self, path: str | Path, settings, cancel: threading.Event | None = None):
        self.path, self.settings, self.cancel = Path(path), settings, cancel
        self._lock = threading.Lock()
        self._stamp: object = object()  # never a file's: the first use makes everything
        self._values: dict[str, str] = {}
        self._providers: tuple = ()
        self._search = None
        self._embedders: dict[tuple, object] = {}  # each made once: it remembers a server that failed, or runs one

    def _fresh(self) -> None:
        """Everything made again when the settings file has changed (or appeared, or gone)."""
        try:
            stamp: object = self.path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp, self._values = stamp, model_link.read(self.path)
        self._providers = (self._provider(0), self._provider(1))
        self._search = self._search_model()

    @property
    def kind(self) -> str:
        with self._lock:
            self._fresh()
            return model_link.in_use(self._values)

    def provider(self, role: int):
        with self._lock:
            self._fresh()
            return self._providers[role]

    def embedder(self):
        with self._lock:
            self._fresh()
            return self._search

    def _provider(self, role: int):
        s, values = self.settings, self._values
        kind = model_link.in_use(values)
        if kind == "cloud":
            return CloudProvider(values.get(model_link.CLOUD_MODEL, ""), values.get(model_link.CLOUD_KEY, ""),
                                 values.get(model_link.CLOUD_URL, ""), thinking=s.assistant_thinking, cancel=self.cancel)
        if kind == "kaggle":
            url, key, place = values.get(model_link.KAGGLE_URL, ""), values.get(model_link.KAGGLE_PASSWORD, ""), "Kaggle"
        else:
            url, key, place = model_link.local_url(values, s.llamacpp_url), "", None
        return LlamaCppProvider(s.llamacpp_model, url, s.assistant_thinking, slot=role, max_tokens=s.assistant_max_tokens,
                                cancel=self.cancel, thinking_budget=s.assistant_thinking_budget or None,
                                api_key=key, place=place)

    def _search_model(self):
        s, values = self.settings, self._values
        if model_link.in_use(values) == "kaggle":
            self._stop_here()  # this computer runs nothing
            if not model_link.ready("kaggle", values):
                return self._once(("none",), NoEmbedder)
            url = values[model_link.KAGGLE_URL].rstrip("/") + model_link.EMBED
            key = values[model_link.KAGGLE_PASSWORD]
            return self._once(("kaggle", url, key), lambda: ServerEmbedder(url, search_model.NAME, api_key=key))
        here = search_model.here(values)
        if here:  # set up from Settings...
            return self._once(("here", *here), lambda: local_embedder(
                *here, s.embed_url, Path(s.assistant_dir) / "embed-server.log"))
        if getattr(s, "embed_gguf", ""):  # ... or in the environment
            return self._once(("here",), lambda: make_embedder(s))
        return self._once(("none",), NoEmbedder)

    def _stop_here(self) -> None:
        """The search server this computer runs, stopped while Kaggle is in use (a switch back starts it again)."""
        for key, embedder in self._embedders.items():
            if key[0] == "here" and getattr(embedder, "server", None) is not None:
                embedder.server.close()

    def _once(self, key: tuple, make):
        if key not in self._embedders:
            self._embedders[key] = make()
        return self._embedders[key]


class Live:
    """One role's provider (0: main, 1: the helpers) - whichever the Model settings say now: every attribute is
    the current provider's."""

    def __init__(self, connection: Connection, role: int):
        self._connection, self._role = connection, role

    def __getattr__(self, name: str):
        return getattr(self._connection.provider(self._role), name)


class LiveEmbedder:
    """The search model in use now: every attribute is the current one's (its name, which tags the vectors it makes,
    among them - a different one means the files are vectorised again)."""

    def __init__(self, connection: Connection):
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._connection.embedder(), name)
