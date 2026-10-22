# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The model in use, as Zen's Model settings say now (app/model_link.py): main's and the helpers' providers, and the
search model - made again when the settings file changes (its time stamp, looked at on each use), so a change from
the app applies on the next request, with no restart.

The app holds stand-ins (Live, LiveEmbedder) that pass every use on to what is current. Search by meaning, in order:
EmbeddingGemma on this computer when it is set up (ASSISTANT_EMBED_*); else the Kaggle notebook's, when Kaggle is in
use and the notebook runs one - kept, by name, while the notebook can't be reached, so a lost connection means
keyword search for a while rather than every file vectorised again; else none (an older notebook, a llama-server here
without it, a cloud): search by keywords. Both run the same model file, so their vectors are one model's - a switch
between Kaggle and this computer vectorises nothing again.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from app import model_link
from app.providers import LlamaCppProvider
from app.providers.cloud import CloudProvider
from app.rag.embed import NoEmbedder, ServerEmbedder, make_embedder

NOTEBOOK_SEARCH = "embeddinggemma-2-Q8_0"  # the notebook's search model (its EMBED_FILE), named while it is away
SEARCH_RECHECK = 60  # seconds before a notebook that didn't answer about its search model is asked again


class Connection:
    def __init__(self, path: str | Path, settings, cancel: threading.Event | None = None):
        self.path, self.settings, self.cancel = Path(path), settings, cancel
        self._lock = threading.Lock()
        self._stamp: object = object()  # never a file's: the first use makes everything
        self._values: dict[str, str] = {}
        self._providers: tuple = ()
        self._search = None  # made on the first search after a change (asking the notebook takes a moment)
        self._search_until = 0.0  # a guess about the notebook's search model holds until then
        self._embedders: dict[tuple, object] = {}  # each made once: it remembers a server that failed

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
        self._search, self._search_until = None, 0.0

    @property
    def kind(self) -> str:
        with self._lock:
            self._fresh()
            return model_link.in_use(self._values)

    def provider(self, role: int):
        with self._lock:
            self._fresh()
            return self._providers[role]

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

    def embedder(self):
        with self._lock:
            self._fresh()
            if self._search is not None and time.monotonic() < self._search_until:
                return self._search
            values = dict(self._values)
        made = self._choose_search(values)  # outside the lock: it may ask the notebook
        with self._lock:
            if self._values == values:
                self._search = made
            return made

    def _choose_search(self, values: dict[str, str]):
        forever = float("inf")
        if getattr(self.settings, "embed_gguf", ""):  # this computer's EmbeddingGemma comes first
            self._search_until = forever
            return self._once(("here",), lambda: make_embedder(self.settings))
        if model_link.in_use(values) == "kaggle" and model_link.ready("kaggle", values):
            found = model_link.search_at(values)
            if found is not None:  # the notebook runs it
                url, key, name = found
                self._search_until = forever
                return self._once(("kaggle", url, key, name),
                                  lambda: ServerEmbedder(url, name.removesuffix(".gguf"), api_key=key))
            if not model_link.check("kaggle", values, timeout=10)[0]:  # away for now: keep its model's name
                url = values[model_link.KAGGLE_URL].rstrip("/") + model_link.EMBED
                key = values[model_link.KAGGLE_PASSWORD]
                self._search_until = time.monotonic() + SEARCH_RECHECK
                return self._once(("kaggle", url, key, NOTEBOOK_SEARCH),
                                  lambda: ServerEmbedder(url, NOTEBOOK_SEARCH, api_key=key))
        self._search_until = forever  # an older notebook (Qwen only), a llama-server here, a cloud: keywords
        return self._once(("none",), NoEmbedder)

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
