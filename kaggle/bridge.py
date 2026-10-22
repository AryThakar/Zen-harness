# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Bridge: makes llama-server on Kaggle look like a local one, so Zen runs unchanged.

Listens where Zen expects its model (127.0.0.1:8080) and passes every request to the Kaggle notebook's
address with its password, streaming answers back as they come. Zen's Stop button closes the request,
and the bridge then closes Kaggle's, which stops the model there too.

    1. Run the Kaggle notebook (zen-qwen36-server.ipynb); its last cell prints an address and a password.
    2. Put them in bridge.env next to this file:
           KAGGLE_URL=https://....trycloudflare.com
           KAGGLE_PASSWORD=...
    3. python bridge.py            (then start Zen as usual: it finds "llama-server" at 127.0.0.1:8080)

The settings file is read again whenever it changes: a new notebook session's address and password, saved from
Zen's model settings (app/model_link.py) or by hand, take effect on the next request - no restart. ZEN_MODEL_SETTINGS
names another file (Zen-harness.exe's settings.env).

Standard library only. Only for the model on Kaggle: a llama-server on this computer listens at 127.0.0.1:8080
itself, and needs no bridge.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LISTEN = ("127.0.0.1", 8080)
TIMEOUT = 600  # longest silence while an answer streams (a long first prompt), as in Zen
USER_AGENT = "zen-kaggle-bridge/1.0 (+https://github.com/AryThakar/Zen-harness)"
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "host"}


SETTINGS = Path(os.environ.get("ZEN_MODEL_SETTINGS") or HERE / "bridge.env")


def read_settings(path: Path = SETTINGS) -> tuple[str, str]:
    """KAGGLE_URL and KAGGLE_PASSWORD from the settings file, over the environment (as Zen-harness.exe passes them);
    the command line (bridge.py URL PASSWORD) over both. ('', '') when there are none."""
    values = {name: os.environ[name] for name in ("KAGGLE_URL", "KAGGLE_PASSWORD") if os.environ.get(name)}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.strip().startswith("#"):
                values[name.strip()] = value.strip()
    except OSError:
        pass
    url = sys.argv[1] if len(sys.argv) > 1 else values.get("KAGGLE_URL", "")
    password = sys.argv[2] if len(sys.argv) > 2 else values.get("KAGGLE_PASSWORD", "")
    return url.rstrip("/"), password


class Link:
    """Where requests go, read again when the settings file changes (its time stamp): a new address from Zen's model
    settings takes effect on the next request."""
    lock = threading.Lock()
    stamp: float | None = None
    upstream, password = "", ""

    @classmethod
    def current(cls) -> tuple[str, str]:
        try:
            stamp = SETTINGS.stat().st_mtime
        except OSError:
            stamp = None
        with cls.lock:
            if stamp != cls.stamp:
                cls.stamp = stamp
                upstream, password = read_settings()
                if upstream.startswith(("https://", "http://")) and password:
                    if (upstream, password) != (cls.upstream, cls.password) and cls.upstream:
                        print(f"{time.strftime('%H:%M:%S')}  now -> {upstream}", flush=True)
                    cls.upstream, cls.password = upstream, password
            return cls.upstream, cls.password


class Bridge(BaseHTTPRequestHandler):
    def log_message(self, *args):  # one line per request instead (below)
        pass

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_OPTIONS(self):
        self.forward()

    def forward(self):
        started = time.monotonic()
        upstream, password = Link.current()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"authorization", "user-agent"}}
        headers.update({"Authorization": f"Bearer {password}", "User-Agent": USER_AGENT})
        request = urllib.request.Request(upstream + self.path, data=body, headers=headers, method=self.command)
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            response = exc  # llama-server's own error (its status and message) goes back as it is
        except (urllib.error.URLError, OSError) as exc:
            self.answer(502, {"error": {"message": f"The Kaggle server can't be reached ({exc}); is the notebook running "
                                                   "and its address current? (Zen's model settings change it)"}})
            self.note(502, started)
            return
        with response:
            status = getattr(response, "status", None) or response.code
            stream = "text/event-stream" in (response.headers.get("Content-Type") or "")
            try:
                self.send_response(status)
                for name, value in response.headers.items():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                self.send_header("X-Model-Host", "Kaggle")  # Zen says where the model runs (not this computer)
                if stream:  # line by line as Kaggle sends it; the end of the answer closes the connection
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for line in response:
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    data = response.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Zen stopped listening (Stop): leaving this closes Kaggle's side too
        self.note(status, started)

    def answer(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("X-Model-Host", "Kaggle")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def note(self, status: int, started: float):
        print(f"{time.strftime('%H:%M:%S')}  {self.command:4} {self.path.split('?')[0]:40} {status}  "
              f"{time.monotonic() - started:6.1f} s", flush=True)


def main():
    upstream, password = Link.current()
    if not upstream or not password:
        sys.exit(f"Put KAGGLE_URL=https://... and KAGGLE_PASSWORD=... in {SETTINGS}")
    try:  # say up front whether Kaggle answers, and with which model
        request = urllib.request.Request(upstream + "/props", headers={
            "Authorization": f"Bearer {password}", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            props = json.loads(response.read())
        model = Path(props.get("model_path", "?").replace("\\", "/")).name
        vision = (props.get("modalities") or {}).get("vision")
        print(f"Kaggle is up: {model} (vision: {vision}, slots: {props.get('total_slots')})")
    except Exception as exc:  # noqa: BLE001 - still start: the notebook may come up later
        print(f"Kaggle doesn't answer yet ({exc}); requests will fail until it does.")
    server = ThreadingHTTPServer(LISTEN, Bridge)
    print(f"Bridge: http://{LISTEN[0]}:{LISTEN[1]} -> {upstream}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
