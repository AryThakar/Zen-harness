from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_retry_pauses(monkeypatch):
    """The model provider tries a failed connection again after pauses (app/providers/llamacpp.py): none in tests."""
    from app.providers import llamacpp
    monkeypatch.setattr(llamacpp, "RETRY_PAUSES", (0.0, 0.0))


@pytest.fixture(autouse=True)
def no_real_browser_tries(request, monkeypatch):
    """A page written is tried in a real browser (browser.try_page): not in tests, unless a test asks for it."""
    if "real_browser" not in request.keywords:
        from app.tools import browser
        monkeypatch.setattr(browser, "try_page", lambda html, shot=None: None)


@pytest.fixture
def write_file(tmp_path):
    def _write(text: str, name: str = "upload.csv", encoding: str = "utf-8") -> Path:
        path = tmp_path / name
        path.write_bytes(text.encode(encoding))
        return path

    return _write
