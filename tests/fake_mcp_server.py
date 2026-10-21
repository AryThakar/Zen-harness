# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""A stand-in for Playwright MCP over stdio, for tests: browser_navigate and browser_evaluate answer the way it
does (a '### Page' block; a '### Result' JSON value), and one tool fails."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake-browser")
opened = {"url": "about:blank"}


@server.tool()
def browser_navigate(url: str) -> str:
    opened["url"] = url
    return f"### Ran Playwright code\n```js\nawait page.goto('{url}');\n```\n### Page\n- Page URL: {url}\n- Page Title: Fake page"


@server.tool()
def browser_evaluate(function: str) -> str:
    html = f"<html><head><title>Fake page</title></head><body><p>Read from {opened['url']} \"quoted\"</p></body></html>"
    import json
    return f"### Result\n{json.dumps(html)}\n### Ran Playwright code\n```js\nawait page.evaluate('...');\n```"


@server.tool()
def broken() -> str:
    from mcp.server.mcpserver.exceptions import ToolError
    raise ToolError("this tool always fails")


if __name__ == "__main__":
    server.run("stdio")
