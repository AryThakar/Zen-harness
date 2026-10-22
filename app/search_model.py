# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Search by meaning on this computer, set up from Settings: EmbeddingGemma 2 - the same file the Kaggle notebook runs,
so the vectors are one model's and nothing is vectorised again - and the llama-server that runs it.

Files already on this computer are used where they are: in Zen's models folder, or a models folder beside Zen's (a
llama.cpp folder there too, b11515 or later - EmbeddingGemma 2 needs it). The rest is downloaded (about 900 MB in all:
the model, its picture projector, llama.cpp's Vulkan build - it runs on the CPU where Vulkan can't), each checked
against its published SHA-256; a stopped download goes on where it stopped. Then the three paths are written to the
Model settings file (SEARCH_GGUF, SEARCH_MMPROJ, SEARCH_SERVER), and app/connection.py uses them from the next search
on while the model runs on this PC or a cloud (with Kaggle, the notebook's is used: this computer runs nothing).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app import model_link
from app.tools.net import USER_AGENT

GGUF, MMPROJ, SERVER = "SEARCH_GGUF", "SEARCH_MMPROJ", "SEARCH_SERVER"
NAME = "embeddinggemma-2-Q8_0"  # the model's file, here and on the notebook (its EMBED_FILE): the name its vectors carry
BUILD = 11515  # the first llama.cpp that runs EmbeddingGemma 2
MODELS = "https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/main/"
CHUNK = 1 << 20


@dataclass(frozen=True)
class Part:
    name: str
    url: str
    size: int
    sha256: str


# As published (Hugging Face's and GitHub's own SHA-256, 2026-10-23)
MODEL = Part("embeddinggemma-2-Q8_0.gguf", MODELS + "embeddinggemma-2-Q8_0.gguf", 309_855_456,
             "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135")
PROJECTOR = Part("mmproj-embeddinggemma-2-Q8_0.gguf", MODELS + "mmproj-embeddinggemma-2-Q8_0.gguf", 554_821_024,
                 "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f")
LLAMA = Part(f"llama-b{BUILD}-bin-win-vulkan-x64.zip",
             f"https://github.com/ggml-org/llama.cpp/releases/download/b{BUILD}/llama-b{BUILD}-bin-win-vulkan-x64.zip",
             33_444_820, "013275bb02719d6de02107d0e1daca91462bfc6385ac56712f43db8418690358")


class Stopped(Exception):
    pass


def here(values: dict[str, str]) -> tuple[str, str, str] | None:
    """The search model set up on this computer - (model, projector, llama-server) - when its files are all there."""
    paths = tuple(values.get(k, "") for k in (GGUF, MMPROJ, SERVER))
    return paths if all(p and Path(p).is_file() for p in paths) else None  # type: ignore[return-value]


class Setup:
    """One set-up at a time, in the background; the page asks state() as it goes."""

    def __init__(self, settings_path: str | Path, folder: str | Path, near: Path | None = None):
        self.settings_path, self.folder = Path(settings_path), Path(folder)
        self.near = near  # the folder Zen's folder is in: its models\ and llama-b*\ folders are looked in too
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._done = self._total = 0
        self._step, self._error = "", ""

    # ------------------------------------------------------------------ what is here

    def _model_dirs(self) -> list[Path]:
        dirs = [self.folder / "embeddinggemma-2"]
        if self.near is not None:
            dirs.append(self.near / "models" / "embeddinggemma-2")
        return dirs

    def found_file(self, part: Part) -> Path | None:
        return next((d / part.name for d in self._model_dirs() if _is(d / part.name, part.size)), None)

    def found_server(self) -> Path | None:
        own = self.folder / "llama.cpp" / "llama-server.exe"
        if own.is_file():
            return own
        builds = []
        if self.near is not None and self.near.is_dir():
            for d in self.near.iterdir():
                number = re.fullmatch(r"llama-b(\d+)", d.name)
                if number and int(number.group(1)) >= BUILD and (d / "llama-server.exe").is_file():
                    builds.append((int(number.group(1)), d / "llama-server.exe"))
        return max(builds)[1] if builds else None

    def missing(self) -> list[Part]:
        need = [p for p in (MODEL, PROJECTOR) if self.found_file(p) is None]
        return need + ([LLAMA] if self.found_server() is None else [])

    def state(self) -> dict:
        values = model_link.read(self.settings_path)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            done, total, step, error = self._done, self._total, self._step, self._error
        need = [] if running else self.missing()
        return {"ready": here(values) is not None, "running": running, "done_mb": round(done / 1e6),
                "total_mb": round(total / 1e6), "step": step, "error": error,
                "download_mb": round(sum(p.size for p in need) / 1e6)}

    # ------------------------------------------------------------------ setting it up

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._error, self._done, self._total, self._step = "", 0, 0, "Starting…"
            self._thread = threading.Thread(target=self._run, daemon=True, name="search-setup")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _say(self, step: str) -> None:
        with self._lock:
            self._step = step

    def _run(self) -> None:
        try:
            need = self.missing()
            with self._lock:
                self._total = sum(p.size for p in need)
            models = self.folder / "embeddinggemma-2"
            free = shutil.disk_usage(_existing(self.folder)).free
            if free < self._total + 100_000_000:
                raise RuntimeError(f"Not enough free space: {self._total / 1e6:,.0f} MB to download, "
                                   f"{free / 1e6:,.0f} MB free on that drive.")
            for part in need:
                if part is LLAMA:
                    self._say("Downloading llama.cpp…")
                    zipped = self._download(part, self.folder / "llama.cpp")
                    _unpack(zipped, self.folder / "llama.cpp")
                    zipped.unlink()
                else:
                    self._say("Downloading EmbeddingGemma 2…" if part is MODEL else "Downloading its picture part…")
                    self._download(part, models)
            model, projector, server = self.found_file(MODEL), self.found_file(PROJECTOR), self.found_server()
            if not (model and projector and server):
                raise RuntimeError("A file is still missing after the download; try again.")
            model_link.write(self.settings_path, {GGUF: str(model), MMPROJ: str(projector), SERVER: str(server)})
            self._say("Ready")
        except Stopped:
            self._say("Stopped - it goes on from there next time.")
        except Exception as exc:  # noqa: BLE001 - said on the page, whatever it is
            with self._lock:
                self._error, self._step = _plain(exc), ""

    def _download(self, part: Part, into: Path) -> Path:
        """The file, checked; a .part left by a stopped download is carried on (a Range request)."""
        into.mkdir(parents=True, exist_ok=True)
        final, partial = into / part.name, into / (part.name + ".part")
        digest, have = hashlib.sha256(), 0
        if partial.is_file() and partial.stat().st_size < part.size:
            with open(partial, "rb") as f:
                for block in iter(lambda: f.read(CHUNK), b""):
                    digest.update(block)
                    have += len(block)
        headers = {"User-Agent": USER_AGENT, **({"Range": f"bytes={have}-"} if have else {})}
        with urllib.request.urlopen(urllib.request.Request(part.url, headers=headers), timeout=60) as response:
            if have and response.status != 206:  # the server sent it whole: start again
                digest, have = hashlib.sha256(), 0
            self._add(have)
            with open(partial, "ab" if have else "wb") as out:
                for block in iter(lambda: response.read(CHUNK), b""):
                    if self._stop.is_set():
                        raise Stopped
                    out.write(block)
                    digest.update(block)
                    self._add(len(block))
        if digest.hexdigest() != part.sha256:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{part.name} didn't download whole (its checksum is wrong); try again.")
        partial.replace(final)
        return final

    def _add(self, n: int) -> None:
        with self._lock:
            self._done += n


def _is(path: Path, size: int) -> bool:
    try:
        return path.stat().st_size == size
    except OSError:
        return False


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _unpack(zipped: Path, into: Path) -> None:
    """llama-server and the libraries it loads - nothing else of the build."""
    with zipfile.ZipFile(zipped) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if name == "llama-server.exe" or name.lower().endswith(".dll"):
                with z.open(info) as src, open(into / name, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _plain(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"The download was refused ({exc.code}); try again later."
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "The download stopped: is the internet on? Try again - it goes on where it stopped."
    return str(exc)[:300]
