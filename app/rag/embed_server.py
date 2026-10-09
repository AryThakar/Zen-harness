"""The embedding server, run by the app: llama-server with --embeddings and the model's picture projector,
started the first time search or indexing needs it and stopped when the app exits - one less thing to start
by hand. It listens on this computer only (the host and port of ASSISTANT_EMBED_URL).

If something already answers at that address with the same model file (a server left from an earlier run,
or one started by hand), it is used as it is. The GPU is tried first, with llama-server fitting the layers
to the memory that is free (the chat model may be using some); if it can't start there, it starts on the CPU
(slower, the same vectors). A server that can't start at all is reported once in the log; search then runs
on keywords until it can (ServerEmbedder tries again after a pause).
"""

from __future__ import annotations

import atexit
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

START_TIMEOUT = 120  # seconds for the model to load (the first CUDA start on a machine is the slow one)
CONTEXT = 8192  # tokens: EmbeddingGemma's whole window; an embedding needs its input in one batch


class EmbedServer:
    def __init__(self, exe: str, model: str, mmproj: str, url: str, log_path: str | Path):
        self.exe, self.model, self.mmproj = exe, model, mmproj
        parts = urlsplit(url)
        self.host, self.port = parts.hostname or "127.0.0.1", parts.port or 80
        self.url = f"{parts.scheme or 'http'}://{self.host}:{self.port}"
        self.log_path = Path(log_path)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        atexit.register(self.close)

    def command(self, gpu: bool = True) -> list[str]:
        cmd = [self.exe, "-m", self.model, "--embeddings", "--host", self.host, "--port", str(self.port),
               "-c", str(CONTEXT), "-b", str(CONTEXT), "-ub", str(CONTEXT), "--kv-unified", "--no-webui"]
        if self.mmproj:
            cmd += ["--mmproj", self.mmproj]
        return cmd + (["--fit", "on"] if gpu else ["-ngl", "0", "--no-mmproj-offload"])

    def start(self) -> None:
        """Make sure the server answers; start it if nothing does. Raises RuntimeError if it can't."""
        with self._lock:
            if self._answering():
                return
            if not Path(self.exe).is_file():
                raise RuntimeError(f"llama-server not found at {self.exe} (ASSISTANT_EMBED_SERVER)")
            if not Path(self.model).is_file():
                raise RuntimeError(f"the embedding model file {self.model} is missing (ASSISTANT_EMBED_GGUF)")
            errors = []
            for gpu in (True, False):
                try:
                    self._launch(gpu)
                    return
                except RuntimeError as exc:
                    errors.append(f"{'GPU' if gpu else 'CPU'}: {exc}")
                    self._stop()
            raise RuntimeError("the embedding server did not start - " + "; ".join(errors) + f" (log: {self.log_path})")

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _launch(self, gpu: bool) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "ab") as out:
            out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(self.command(gpu))}\n".encode())
            out.flush()
            self._proc = subprocess.Popen(self.command(gpu), stdout=out, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        log.info("Starting the embedding server (%s) at %s", "GPU" if gpu else "CPU", self.url)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"it stopped with code {self._proc.returncode}: {self._log_tail()}")
            if self._answering():
                return
            time.sleep(0.5)
        raise RuntimeError(f"it did not answer within {START_TIMEOUT} s")

    def _answering(self) -> bool:
        """A server with this model file answers at the address. Another model there is an error - its
        vectors would mix with this one's."""
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                if r.status != 200:
                    return False
            with urllib.request.urlopen(self.url + "/props", timeout=5) as r:
                props = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return False
        served = Path(str(props.get("model_path") or "")).name
        if served and served != Path(self.model).name:
            raise RuntimeError(f"{self.url} serves another model ({served}), not {Path(self.model).name}")
        return True

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _log_tail(self, lines: int = 3) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            return ""
        return " | ".join(line.strip() for line in text[-lines:])
