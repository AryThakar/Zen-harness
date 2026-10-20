"""HTTP for the tools that call web services: timeouts, a bounded retry on transient errors only
(429, 408, 5xx, connection failures), and errors that say whether retrying could help.

Each tool takes an optional httpx.Client, so tests pass one with a fake transport and no test
touches the network.
"""

from __future__ import annotations

import time

import httpx

# An honest name with the project's address as the contact: Wikipedia (and its API) refuses
# automated readers without one, also when they claim to be a browser.
USER_AGENT = "Mozilla/5.0 (compatible; ai-assistant-tools/1.0; +https://github.com/AryThakar/Zen-harness)"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRY_WAIT = 10.0


class NetError(Exception):
    def __init__(self, code: str, message: str, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.status = code, message, retryable, status


def client(timeout: float = 10.0) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=4.0), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def get(http: httpx.Client, url: str, params: dict | None = None, attempts: int = 2,
        headers: dict | None = None) -> httpx.Response:
    """GET with up to ``attempts`` tries; only transient failures are retried."""
    for attempt in range(1, attempts + 1):
        try:
            r = http.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            error = NetError("timeout", f"{_host(url)} did not answer in time", True)
            if attempt == attempts:
                raise error from exc
        except httpx.HTTPError as exc:
            error = NetError("unavailable", f"{_host(url)} could not be reached", True)
            if attempt == attempts:
                raise error from exc
        else:
            if r.is_success:
                return r
            retryable = r.status_code in RETRY_STATUS
            if not retryable or attempt == attempts:
                raise NetError("http_error", f"{_host(url)} answered HTTP {r.status_code}", retryable, r.status_code)
            wait = _retry_after(r)
            time.sleep(wait if wait is not None else 0.3 * attempt)
            continue
        time.sleep(0.3 * attempt)
    raise AssertionError("unreachable")


def get_json(http: httpx.Client, url: str, params: dict | None = None, attempts: int = 2) -> dict:
    r = get(http, url, params, attempts)
    try:
        return r.json()
    except ValueError as exc:
        raise NetError("bad_response", f"{_host(url)} sent something that is not JSON", False) from exc


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return min(float(r.headers["Retry-After"]), MAX_RETRY_WAIT)
    except (KeyError, ValueError):
        return None


def _host(url: str) -> str:
    return httpx.URL(url).host or url
