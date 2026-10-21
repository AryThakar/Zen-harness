# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""The MCP client (browser.McpServer) against a stand-in MCP server over stdio (tests/fake_mcp_server.py), and
browser.page's reading of its answers - no Node, no browser, no network."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from app.tools import browser
from app.tools.browser import McpError, McpServer
from app.tools.core import ToolError

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


@pytest.fixture
def server():
    s = McpServer("the fake browser", sys.executable, [FAKE], idle=1.0)
    yield s
    s.close()


def test_a_tool_is_called_and_its_text_comes_back(server):
    opened = server.call("browser_navigate", {"url": "https://example.com/a"})
    assert browser._field(opened, "Page URL") == "https://example.com/a" and browser._field(opened, "Page Title") == "Fake page"
    html = browser._result(server.call("browser_evaluate", {"function": "() => 1"}))
    assert html.startswith("<html>") and 'Read from https://example.com/a "quoted"' in html


def test_a_failing_tool_is_an_error_and_the_server_goes_on(server):
    with pytest.raises(McpError, match="always fails"):
        server.call("broken", {})
    assert "Page URL" in server.call("browser_navigate", {"url": "https://example.com/b"})


def test_an_idle_server_closes_and_the_next_call_starts_it_again(server):
    server.call("browser_navigate", {"url": "https://example.com/c"})
    first = server._thread
    time.sleep(2.5)  # idle 1 s: closed
    assert not first.is_alive()
    assert "Page URL" in server.call("browser_navigate", {"url": "https://example.com/d"})


def test_a_server_that_cannot_start_says_so():
    with pytest.raises(McpError, match="could not start"):
        McpServer("nothing", sys.executable, ["-c", "import sys; sys.exit(3)"]).call("x", {})


def test_page_checks_the_address_before_and_after(monkeypatch, server):
    monkeypatch.setattr(browser, "_server", lambda: server)
    checked = []

    def check(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise ToolError("Addresses on this computer or a private network can't be read.", field="url")
    final, title, html = browser.page("https://example.com/e", check)
    assert (final, title) == ("https://example.com/e", "Fake page") and checked == ["https://example.com/e"] * 2
    with pytest.raises(ToolError):
        browser.page("http://127.0.0.1:8080/props", check)
