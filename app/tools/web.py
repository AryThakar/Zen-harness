"""web_search and read_webpage: find pages, then read one, with the source for every fact.

web_search  - titles, links and snippets from public search engines via the ddgs package (no key).
              Engines are named in two tiers and the second is asked only when the first finds
              nothing; ddgs's 'auto' mode is never used, so only these engines are contacted. ddgs
              reports an empty search by raising, which is told apart from a real failure.
read_webpage - the readable text of one page: HTML reduced to its main content (article / main,
              without scripts, navigation, headers, footers, forms), plain text as is, PDF text by
              pypdf. Only public addresses are fetched - localhost, private networks and link-local
              addresses are refused, and every redirect is checked again - so a page can't make
              the assistant read this computer's own services (llama-server, this app's API).
              Downloads are capped at 3 MB; the text at 12,000 characters.

Design points (engine tiers, the empty-search exception, checking every redirect) from Unsloth
Studio's web tools (studio/backend/core/inference/tools.py), written independently.
Limit: the address is checked when resolved; a server that changes its DNS answer between the
check and the connection (DNS rebinding) is not stopped.
"""

from __future__ import annotations

import io
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import net
from .core import ToolError, ToolResult, ok, tool

TIERS = (("duckduckgo", "brave", "mojeek", "wikipedia"), ("google", "yahoo", "startpage"))
SEARCH_DEADLINE = 15.0
MAX_BYTES = 3 * 1024 * 1024
MAX_TEXT = 12_000
MAX_REDIRECTS = 5
DROP = ("script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "iframe", "button",
        "template", "canvas", "select")
BLOCKS = {"p", "li", "pre", "blockquote", "td", "th", "dt", "dd", "figcaption", "caption", "div", "section"}
CHROME = re.compile(r"\b[\w-]*(?:nav|menu|sidebar|breadcrumb|related|footer|cookie|banner|share|social|advert|"
                    r"promo|newsletter|subscribe|skip-link|toc)[\w-]*\b")


# ---------------------------------------------------------------- web_search

@tool
def web_search(query: str, site: str | None = None, k: int = 5, *, engine=None) -> ToolResult:
    """Search the web; returns titles, links and snippets (read a page before quoting details)."""
    query = " ".join(str(query).split())
    if len(query) < 2:
        raise ToolError("query is empty.", field="query")
    k = max(1, min(int(k), 10))
    domain = _domain(site) if site else None
    full_query = f"{query} site:{domain}" if domain else query
    results, error = _search(engine or _ddgs, full_query, k * 2 if domain else k)
    if domain:
        results = [r for r in results if _host_in(r["url"], domain)]
    results = results[:k]
    if error and not results:
        raise ToolError(error, invalid=False)
    out = {"query": query, **({"site": domain} if domain else {}), "results": results}
    if not results:
        out["note"] = "No results. Try other words, or read a known page with read_webpage."
        return ok(out)
    out["note"] = "These are only snippets: read a page with read_webpage before quoting details, and cite its URL."
    return ok(out, {"type": "links", "query": query, "items": results})


def _search(engine, query: str, k: int) -> tuple[list[dict], str | None]:
    """Ask tier 1, then tier 2 if it found nothing, within one overall deadline."""
    deadline = time.monotonic() + SEARCH_DEADLINE
    error = None
    for tier in TIERS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = engine(query, k, tier, remaining)
        except Exception as exc:  # noqa: BLE001 - classified below; the next tier may still answer
            error = _search_failure(exc)
            continue
        results = []
        for r in raw or []:
            url = str(r.get("href") or r.get("url") or "").strip()
            if url.startswith(("http://", "https://")) and all(x["url"] != url for x in results):
                results.append({"title": " ".join(str(r.get("title") or "").split()), "url": url,
                                "snippet": " ".join(str(r.get("body") or r.get("snippet") or "").split())[:400]})
        if results:
            return results, None
    return [], error


def _search_failure(exc: Exception) -> str | None:
    """None for 'nothing found' (ddgs raises for an empty search), else a message the model can act on."""
    name, text = type(exc).__name__, str(exc)
    if "No results found" in text:
        return None
    if name == "RatelimitException" or "ratelimit" in text.lower():
        return "The search engines are rate limiting this computer; wait a minute, or read a known page with read_webpage."
    if "timeout" in name.lower() or "timed out" in text.lower():
        return "The search engines did not answer in time."
    return f"Search failed ({name})."


def _ddgs(query: str, k: int, tier: tuple[str, ...], timeout: float) -> list[dict]:
    try:
        from ddgs import DDGS
        from ddgs.engines import ENGINES
    except ImportError as exc:
        raise ToolError("Web search is not installed (the ddgs package is missing).", invalid=False) from exc
    available = ENGINES.get("text", {})
    names = [n for n in tier if n in available]
    if not names:
        return []
    return DDGS(timeout=max(1, int(timeout))).text(query, max_results=k, backend=",".join(names))


def _domain(site: str) -> str:
    s = str(site).strip().lower()
    host = urlsplit(s if "//" in s else f"//{s}").hostname or ""
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ToolError(f"site must be a domain like 'python.org', not {site!r}.", field="site")
    return host.removeprefix("www.")


def _host_in(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


# ---------------------------------------------------------------- read_webpage

@tool
def read_webpage(url: str, *, http: httpx.Client | None = None, resolve=None) -> ToolResult:
    """The readable text of one public web page (HTML, plain text or PDF)."""
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            raise ToolError("Only http and https addresses can be read.", field="url")
        url = "https://" + url
    if len(url) > 2048:
        raise ToolError("The address is too long.", field="url")
    http = http or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                                headers={"User-Agent": net.USER_AGENT, "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5"})
    body, ctype, charset, final = _fetch(http, url, resolve or socket.getaddrinfo)
    if ctype == "application/pdf" or body[:5] == b"%PDF-":
        title, text = _pdf_text(body)
    elif "html" in ctype or _looks_html(body):
        title, text = _html_text(body, charset)
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        title, text = None, body.decode(charset or "utf-8", "replace").strip()
    else:
        raise ToolError(f"The page is not text (it is {ctype or 'an unknown type'}).", invalid=False)
    if not text.strip():
        raise ToolError("No readable text was found on the page (it may need JavaScript to show its content).",
                        invalid=False)
    truncated = len(text) > MAX_TEXT
    if truncated:
        text = text[:MAX_TEXT].rsplit(" ", 1)[0] + " ..."
    result = {"url": final, "title": title, "text": text, "words": len(text.split()),
              "note": "Quote only what this page says and cite its URL."}
    if truncated:
        result["truncated"] = f"Only the first {MAX_TEXT:,} characters are shown."
    return ok(result, {"type": "webpage", "url": final, "title": title})


def _fetch(http: httpx.Client, url: str, resolve) -> tuple[bytes, str, str | None, str]:
    """(body, content type, charset or None, final url), following redirects one checked hop at a time."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url, resolve)
        try:
            with http.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if not r.is_success:
                    why = " (the site refuses automated readers; try another source)" if r.status_code in (401, 403, 429) else ""
                    raise ToolError(f"The page answered HTTP {r.status_code}{why}.", invalid=False)
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ToolError("The page is larger than 3 MB.", invalid=False)
                    chunks.append(chunk)
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return b"".join(chunks), ctype, r.charset_encoding, url
        except httpx.TimeoutException as exc:
            raise ToolError("The page did not answer in time.", invalid=False) from exc
        except httpx.HTTPError as exc:
            raise ToolError("The page could not be reached.", invalid=False) from exc
    raise ToolError(f"More than {MAX_REDIRECTS} redirects.", invalid=False)


def _check_public(url: str, resolve) -> None:
    """Refuse addresses on this computer or a private network (also hidden behind a name)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("Only http and https addresses can be read.", field="url")
    host = parts.hostname
    private = ToolError("Addresses on this computer or a private network can't be read.", field="url")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise private
    try:
        ips = [ipaddress.ip_address(host)]  # a literal address needs no lookup
    except ValueError:
        try:
            infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ToolError(f"The address {host} does not exist.", invalid=False) from exc
        ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if any(not ip.is_global or ip.is_multicast for ip in ips):
        raise private


def _looks_html(body: bytes) -> bool:
    head = body[:2000].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<body" in head


def _html_text(body: bytes, charset: str | None) -> tuple[str | None, str]:
    from lxml import html as lxml_html

    try:
        # Without a charset in the headers, lxml reads the page's own <meta charset>.
        doc = lxml_html.document_fromstring(body.decode(charset, "replace") if charset else body)
    except Exception:  # noqa: BLE001 - malformed beyond repair
        return None, ""
    title = " ".join((doc.findtext(".//title") or "").split()) or None
    for el in doc.xpath("|".join(f"//{t}" for t in DROP) + "|//*[@hidden]|//*[@aria-hidden='true']"
                        "|//*[@role='navigation' or @role='banner' or @role='contentinfo' or @role='complementary'"
                        " or @role='search']|//a[contains(@class, 'headerlink')]"):
        el.drop_tree()
    candidates = (doc.xpath("//article") or doc.xpath("//main") or doc.xpath("//*[@role='main']")
                  or doc.xpath("//body") or [doc])
    root = max(candidates, key=lambda e: len(e.text_content()))
    # Menus and sidebars marked only by class or id ('sidebar', 'related', 'breadcrumb' ...) - unless
    # the element holds most of the page's text, which makes it the content, whatever its name.
    total = len(root.text_content()) or 1
    for el in root.xpath(".//*[self::div or self::section or self::ul or self::ol or self::table][@class or @id]"):
        names = f"{el.get('class', '')} {el.get('id', '')}".lower()
        if CHROME.search(names) and el.getparent() is not None and len(el.text_content()) < 0.3 * total:
            el.drop_tree()
    lines: list[str] = []
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(el.text_content().split())
            if text:
                lines.append(f"\n{'#' * int(tag[1])} {text}")
        elif tag in BLOCKS and not any(isinstance(c.tag, str) and c.tag in BLOCKS | {"h1", "h2", "h3", "ul", "ol", "table"}
                                       for c in el):
            text = " ".join(el.text_content().split())
            if text:
                lines.append(("- " if tag == "li" else "") + text)
    text = "\n".join(dict.fromkeys(lines)).strip()  # dict: drops repeated lines (menus copied twice)
    if not text:
        text = " ".join(root.text_content().split())
    return title, text


def _pdf_text(body: bytes) -> tuple[str | None, str]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [p.extract_text() or "" for p in reader.pages[:30]]
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for broken files
        raise ToolError("The PDF could not be read.", invalid=False) from exc
    title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    return (str(title) if title else None), "\n\n".join(f"[page {i + 1}]\n{t.strip()}" for i, t in enumerate(pages) if t.strip())
