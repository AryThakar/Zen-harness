"""web_search and read_webpage: find pages, then read one, with the source for every fact.

web_search  - titles, links and snippets from public search engines via the ddgs package (no key);
              recent (day, week, month, year) uses the engines' own time filter, for "latest" questions.
              Engines are named in two tiers and the second is asked only when the first finds
              nothing; ddgs's 'auto' mode is never used, so only these engines are contacted. ddgs
              reports an empty search by raising, which is told apart from a real failure.
read_webpage - the readable text of one page: HTML reduced to its main content (article / main,
              without scripts, navigation, headers, footers, forms), plain text as is, PDF text by
              pypdf. Only public addresses are fetched - localhost, private networks and link-local
              addresses are refused, and every redirect is checked again - so a page can't make
              the assistant read this computer's own services (llama-server, this app's API).
              Downloads are capped at 3 MB; the text at 12,000 characters.
              GitHub pages are read through GitHub's API instead (a repository's description, main
              language, license, its file list and README; a folder's files; a file's raw text): the HTML pages are mostly
              interface and answer 503 now and then. Without a token the API allows 60 calls an
              hour; past that a file comes from raw.githubusercontent.com and a repository page is
              read as HTML.
              YouTube videos are read from their public watch page: title, channel, length, date,
              views, category and the full description (YouTube's oEmbed - title and channel - when
              the page doesn't have them). What is said in a video is not available: captions are
              only served to YouTube's own player, which proves it is a real app.
              For the UI, each page also yields its card: title, site name, description, preview
              image (og:image), favicon, author and published date, read from the page's <head>.
look       - what a link holds (name, type, size), read from its first bytes, for the user to
              approve before anything is downloaded (app/tools/approval.py); then fetch_table stores
              a data file (CSV, TSV, Excel) from any public link as a table, and fetch_file one file
              from GitHub like an upload. The same address checks, a 20 MB cap; a web page instead
              of a file is refused with the files it links to.
fetch_icon / fetch_image - a site's favicon or a page's preview image, fetched by the server with
              the same public-address checks (size-capped, verified to be an image), so the UI shows
              them from this app instead of the browser contacting every site.

Design points (engine tiers, the empty-search exception, checking every redirect) from Unsloth
Studio's web tools (studio/backend/core/inference/tools.py), written independently.
Limit: the address is checked when resolved; a server that changes its DNS answer between the
check and the connection (DNS rebinding) is not stopped.
"""

from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
import logging
import tempfile
import threading
import time
from contextlib import nullcontext
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from . import filetypes, net
from .core import ToolError, ToolResult, ok, tool

log = logging.getLogger(__name__)
TIERS = (("duckduckgo", "brave", "mojeek", "wikipedia"), ("google", "yahoo", "startpage"))
SEARCH_DEADLINE = 15.0
MAX_BYTES = 3 * 1024 * 1024
MAX_TEXT = 12_000
MAX_REDIRECTS = 5
DROP = ("script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "iframe", "button",
        "template", "canvas", "select")
BLOCKS = {"p", "li", "pre", "blockquote", "td", "th", "dt", "dd", "figcaption", "caption", "div", "section"}
# A cookie notice, by what it says: a short block about cookies with a consent word (seen: Kaggle's, a plain div
# with generated class names, opening every page read through the browser). A short line about baking cookies
# that mentions a privacy setting would go too - a cost accepted for one rule that fits every site.
COOKIE_NOTICE = re.compile(r"\bcookies?\b.{0,200}\b(?:accept|agree|consent|got it|learn more|privacy|preferences|"
                           r"settings|reject|allow)\b", re.I | re.S)
CHROME = re.compile(r"\b[\w-]*(?:nav|menu|sidebar|breadcrumb|related|footer|cookie|banner|share|social|advert|"
                    r"promo|newsletter|subscribe|skip-link|toc)[\w-]*\b")


# ---------------------------------------------------------------- what this chat has read

# A helper remembers nothing between tasks, so the chat does, for a while: a page read in it comes back from memory
# (same address, same find words), and a site that refused is refused at once with its earlier reason. Measured:
# after leetcode.com refused (a bot check), the next two tasks of the same chat went to leetcode.com again and read
# algo.monster again - minutes each. In memory only: a restart forgets, and a page is read afresh after
# PAGE_MEMORY_HOURS.
PAGE_MEMORY_HOURS = 12
PAGES_KEPT = 40  # per chat
_seen: dict[str, dict] = {}
_seen_lock = threading.Lock()


def _chat_memory(chat: str) -> dict:
    now = time.time()
    with _seen_lock:
        mem = _seen.setdefault(chat, {"pages": {}, "refused": {}})
        for kind in ("pages", "refused"):
            mem[kind] = {k: v for k, v in mem[kind].items() if now - v[0] < PAGE_MEMORY_HOURS * 3600}
        return mem


def remembered_page(chat: str, url: str, find: str | None) -> ToolResult | None:
    """This chat's earlier read of the page (with the same find words), or None."""
    got = _chat_memory(chat)["pages"].get((url.strip(), (find or "").strip().lower()))
    if not got:
        return None
    when, result, artifact = got
    note = f"Read earlier in this chat ({time.strftime('%H:%M', time.localtime(when))}): the same page, from memory."
    return ToolResult("ok", {**result, "note": " ".join(filter(None, [note, result.get("note")]))}, artifact)


def refused_site(chat: str, url: str) -> ToolResult | None:
    """A site that refused this chat earlier: refused again at once, with its reason - or None."""
    host = (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")
    got = _chat_memory(chat)["refused"].get(host)
    if not got:
        return None
    when, why = got
    return ToolResult("failed", {"error": {"message": (
        f"{host} refused reading earlier in this chat ({time.strftime('%H:%M', time.localtime(when))}: "
        f"{why.rstrip('.')}). Read another source."), "reason": "blocked"}})


def remember_read(chat: str, url: str, find: str | None, result: ToolResult) -> None:
    """A read's outcome kept for the chat: the page (read), or its site (refused)."""
    mem = _chat_memory(chat)
    with _seen_lock:
        if result.status in ("ok", "partial"):
            mem["pages"][(url.strip(), (find or "").strip().lower())] = (time.time(), result.result, result.artifact)
            for old in sorted(mem["pages"], key=lambda k: mem["pages"][k][0])[:-PAGES_KEPT]:
                mem["pages"].pop(old, None)
        elif (result.result.get("error") or {}).get("reason") == "blocked":
            host = (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")
            mem["refused"][host] = (time.time(), str(result.result["error"].get("message", "it refused")))


def seen_in_chat(chat: str) -> str:
    """One line each for the web helper's opening: the pages this chat has read, and the sites that refused."""
    mem = _chat_memory(chat)
    pages = list(dict.fromkeys(f"{r.get('title') or url} ({url})" for (url, _), (_, r, _a) in mem["pages"].items()))
    parts = []
    if pages:
        parts.append("Pages read earlier in this chat (read one again and it comes from memory): " + "; ".join(pages[-8:]))
    if mem["refused"]:
        parts.append("Sites that refused reading in this chat: " + "; ".join(
            f"{h} ({why.split(':')[0][:60]})" for h, (_, why) in mem["refused"].items()))
    return "\n".join(f"[{p}]" for p in parts)


# ---------------------------------------------------------------- web_search

RECENT = {"day": "d", "week": "w", "month": "m", "year": "y"}  # ddgs's timelimit codes


@tool
def web_search(query: str, site: str | None = None, k: int = 5, recent: str | None = None, *,
               engine=None, embedder=None) -> ToolResult:
    """Search the web; returns titles, links and snippets (read a page before quoting details), in the
    engines' order fused with meaning when the embedding model runs (_by_meaning)."""
    query = " ".join(str(query).split())
    if len(query) < 2:
        raise ToolError("query is empty.", field="query")
    if recent is not None and recent not in RECENT:
        raise ToolError(f"recent must be one of {', '.join(RECENT)}.", field="recent")
    k = max(1, min(int(k), 10))
    domain = _domain(site) if site else None
    full_query = f"{query} site:{domain}" if domain else query
    results, error = _search(engine or _ddgs, full_query, k * 2 if domain else k, RECENT.get(recent))
    if domain:
        results = [r for r in results if _host_in(r["url"], domain)]
    results = _by_meaning(query, results[:k], embedder)
    if error and not results:
        raise ToolError(error, invalid=False)
    out = {"query": query, **({"site": domain} if domain else {}), **({"recent": recent} if recent else {}),
           "results": results}
    if not results:
        out["note"] = "No results. Try other words, or read a known page with read_webpage."
        return ok(out)
    out["note"] = "These are only snippets: read a page with read_webpage before quoting details."
    items = [{**r, "image": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg", "video": v} if (v := youtube_id(r["url"])) else r
             for r in results]  # video results show their thumbnail in the UI
    return ok(out, {"type": "links", "query": query, "items": items})


RRF_K = 60  # as the search index fuses its rankings


def _by_meaning(query: str, results: list[dict], embedder) -> list[dict]:
    """The engines' order fused by rank with meaning (the query against each result's title and snippet),
    so the results worth reading come first. Measured on the user's own searches (bench/web_rank.py, 47
    searches whose answer cited a result): the first cited result in the top 3 for 91% against 87% in the
    engines' order, and first for 47% against 45% - on data that favours the engines' order (the helper
    mostly read their top results); meaning alone did worse (83%). Without the model: the engines' order."""
    if embedder is None or len(results) < 2:
        return results
    from app.rag.embed import EmbedderUnavailable
    try:
        q = embedder.embed_query(query)
        docs = embedder.embed_documents([r["snippet"] or r["title"] for r in results], [r["title"] for r in results])
    except EmbedderUnavailable:
        return results
    by_meaning = sorted(range(len(results)), key=lambda i: -float(docs[i] @ q))
    place = {i: n for n, i in enumerate(by_meaning)}
    score = [1 / (RRF_K + i + 1) + 1 / (RRF_K + place[i] + 1) for i in range(len(results))]
    return [results[i] for i in sorted(range(len(results)), key=lambda i: (-score[i], i))]


def _search(engine, query: str, k: int, timelimit: str | None = None) -> tuple[list[dict], str | None]:
    """Ask tier 1, then tier 2 if it found nothing, within one overall deadline."""
    deadline = time.monotonic() + SEARCH_DEADLINE
    error = None
    for tier in TIERS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = engine(query, k, tier, remaining, timelimit) if timelimit else engine(query, k, tier, remaining)
        except Exception as exc:  # noqa: BLE001 - classified below; the next tier may still answer
            error = _search_failure(exc)
            continue
        results, seen = [], set()
        for r in raw or []:
            url = str(r.get("href") or r.get("url") or "").strip()
            if url.startswith(("http://", "https://")) and page_key(url) not in seen:
                seen.add(page_key(url))
                results.append({"title": " ".join(str(r.get("title") or "").split()), "url": url,
                                "snippet": " ".join(str(r.get("body") or r.get("snippet") or "").split())[:400],
                                "site": _site(url), "favicon": urljoin(url, "/favicon.ico")})
        if results:
            return results, None
    return [], error


TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|msclkid|ref|ref_src)$", re.I)


def page_key(url: str) -> str:
    """One key per page: the same address with or without www., a trailing slash, a #part or tracking tags
    (utm_source ...) is the same page - two results of one search were often one page twice."""
    parts = urlsplit(url.strip())
    query = "&".join(sorted(q for q in parts.query.split("&") if q and not TRACKING.match(q.split("=", 1)[0])))
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/') or '/'}" + (f"?{query}" if query else "")


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


def _ddgs(query: str, k: int, tier: tuple[str, ...], timeout: float, timelimit: str | None = None) -> list[dict]:
    try:
        from ddgs import DDGS
        from ddgs.engines import ENGINES
    except ImportError as exc:
        raise ToolError("Web search is not installed (the ddgs package is missing).", invalid=False) from exc
    available = ENGINES.get("text", {})
    names = [n for n in tier if n in available]
    if not names:
        return []
    return DDGS(timeout=max(1, int(timeout))).text(query, max_results=k, backend=",".join(names),
                                                   **({"timelimit": timelimit} if timelimit else {}))


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
def read_webpage(url: str, find: str | None = None, *, http: httpx.Client | None = None,
                 resolve=None, browser=None) -> ToolResult:
    """The readable text of one public web page (HTML, plain text or PDF). A page longer than 12,000
    characters is cut; with ``find`` the parts about those words are shown instead."""
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            raise ToolError("Only http and https addresses can be read.", field="url")
        url = "https://" + url
    if len(url) > 2048:
        raise ToolError("The address is too long.", field="url")
    resolve = resolve or socket.getaddrinfo
    if http is None:  # a real read: from the cache, or paced, with a client made (and closed) here
        cached = _cached(url)
        if cached is None:
            _pace(urlsplit(url).hostname or "")
            from . import browser as real_browser
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                              headers={"User-Agent": net.USER_AGENT,
                                       "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5"}) as own:
                cached = _keep(url, _page(own, url, resolve, browser or (real_browser.page if real_browser.available()
                                                                          else None)))
        title, text, meta, final = cached[0], cached[1], dict(cached[2]), cached[3]
    else:
        title, text, meta, final = _page(http, url, resolve, browser)
    if not text.strip():
        raise ToolError("No readable text was found on the page (it may need JavaScript to show its content).",
                        invalid=False, reason="unsupported")
    truncated = len(text) > MAX_TEXT
    shown = None
    if truncated and find and find.strip():
        text, shown = _focus(text, find)
    elif truncated:
        text = text[:MAX_TEXT].rsplit(" ", 1)[0] + " ..."
    files = meta.pop("files", None)
    note = meta.pop("note", None)
    data_table = meta.pop("data_table", None)
    status = "partial" if meta.pop("partial", False) else "cut" if truncated else "ok"
    result = {"url": final, "read": status, "title": title, "site": meta["site"],
              **{k: meta[k] for k in ("published", "updated", "author", "via") if meta.get(k)},
              **({"age": age} if (age := page_age(meta.get("published"), meta.get("updated"))) else {}),
              "text": text, "words": len(text.split())}
    if files:  # main can download these with the user's approval; the web helper never does
        result["files"] = files
        result["files_note"] = "Files this page links to: if the task wants one, give its link in downloads."
    if note:
        result["note"] = note
    if data_table:
        result["data_table"] = (f"{DATA_TABLE_NOTE} ({data_table['rows']} rows: {', '.join(data_table['columns'])}). If "
                                "the task needs its numbers, give this page's link in downloads: main brings the table in "
                                "as data, exact. Don't copy its rows into your answer.")
    if shown:
        result["shown"] = shown
    elif truncated:
        result["truncated"] = (f"The page is long: only the first {MAX_TEXT:,} characters are shown. If what you need "
                               "is not here, call read_webpage again with find='a few words to look for'.")
    return ok(result, {"type": "webpage", "url": final, "title": title or meta["site"], **meta})


PAGE_TTL = 3600  # seconds a page read stays cached: a repeat in the hour costs nothing and asks the site nothing
SITE_GAP = 1.0  # seconds between two page reads of one site (a site that checks for bots counts quick visits)
_pages: dict[str, tuple[float, tuple]] = {}
_last_read: dict[str, float] = {}
_web_lock = threading.Lock()


def _cached(url: str) -> tuple | None:
    with _web_lock:
        hit = _pages.get(url)
        return hit[1] if hit and time.monotonic() - hit[0] < PAGE_TTL else None


def _keep(url: str, page: tuple) -> tuple:
    with _web_lock:
        if len(_pages) > 200:
            _pages.clear()
        _pages[url] = (time.monotonic(), page)
    return page


def _pace(host: str) -> None:
    """Wait until SITE_GAP has passed since this site's last page read."""
    with _web_lock:
        wait = _last_read.get(host, 0.0) + SITE_GAP - time.monotonic()
        _last_read[host] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


# Pages a site shows automated readers instead of its content (Cloudflare, reCAPTCHA, Imperva,
# PerimeterX ...). Seen: "Verifying your browser… This takes less than a second." read as a source.
# Such a page is not read past - it is reported, and the worker reads another source.
BOT_CHECK = re.compile(
    r"verifying (?:your browser|you are (?:a )?human)|checking (?:your|if the site connection is) |just a moment"
    r"|attention required|are you (?:a robot|human)|not a robot|captcha|security check|verify you are human"
    r"|enable javascript and cookies to continue|ddos protection|pardon our interruption|press & hold"
    r"|request unsuccessful|access denied", re.I)
BOT_CHECK_WORDS = 150  # a real page that mentions a captcha is longer than a bot check


def _bot_check(title: str | None, text: str, url: str) -> bool:
    if len(text.split()) >= BOT_CHECK_WORDS:
        return False
    return bool(BOT_CHECK.search(f"{title or ''}\n{text}") or re.search(r"captcha|/cdn-cgi/challenge", url, re.I))


THIN = 20  # words: an HTML page with less is likely drawn by JavaScript (the same bar as a "partial" read)


def _page(http: httpx.Client, url: str, resolve, browser=None) -> tuple[str | None, str, dict, str]:
    """(title, text, card, final url): GitHub and YouTube through their own routes, anything else as is. A
    wiki page (/wiki/...) that refuses the plain read - an HTTP 403 or a bot check - is read through the
    site's own MediaWiki API instead: one general rule, not a list of sites (measured: Fandom answers its
    pages with Cloudflare's 'Just a moment' and a 403, and its api.php with the page). Then the browser
    (browser.py), when there is one, by one general test too: the plain read was refused, met a bot check,
    or an HTML page gave less than THIN words."""
    special = _from_github(http, url, resolve) or _from_youtube(http, url, resolve)
    if special:
        return (*special, url)
    try:
        title, text, meta, final = _read(http, url, resolve)
        if _bot_check(title, text, final):
            raise ToolError("The site showed a bot check instead of its page (it only serves people in a browser); "
                            "read another source.", invalid=False, reason="blocked")
        if meta.pop("_html", False) and browser is not None and (meta.get("partial") or len(text.split()) < THIN):
            seen = _from_browser(browser, url, resolve)
            if seen is not None and len(seen[1].split()) > len(text.split()):
                return seen
        return title, text, meta, final
    except ToolError as exc:
        if exc.reason != "blocked":
            raise
        wiki = _from_wiki(http, url, resolve)
        if wiki is not None:
            return (*wiki, url)
        seen = _from_browser(browser, url, resolve) if browser is not None else None
        if seen is None:
            raise
        return seen


def _from_browser(browser, url: str, resolve) -> tuple[str | None, str, dict, str] | None:
    """The page as the browser shows it, through the same extraction as a plain read; None when the browser
    can't run (the plain read's outcome stands). A bot check in the browser too is a blocked page."""
    from .browser import McpError
    try:
        final, shown_title, html = browser(url, lambda address: _check_public(address, resolve))
    except McpError as exc:
        log.warning("the browser could not read %s: %s", url, exc)
        return None
    body = html.encode("utf-8")
    title, text, page_meta = _html_text(body, "utf-8", final)
    title = title or shown_title
    if _bot_check(title, text, final):
        raise ToolError("The site showed a bot check even to a browser (it only serves people); read another source.",
                        invalid=False, reason="blocked")
    meta = {"site": _site(final), "favicon": urljoin(final, "/favicon.ico"), **page_meta, "via": "a browser"}
    files = offered_files(body, final)
    if files:
        meta["files"] = files
    return title, text, meta, final


WIKI_PAGE = re.compile(r"^/wiki/([^?#]+)")
WIKI_APIS = ("/api.php", "/w/api.php")  # where MediaWiki sites keep it: Fandom's, then Wikipedia's layout


def _from_wiki(http: httpx.Client, url: str, resolve) -> tuple[str | None, str, dict] | None:
    """A /wiki/ page through its site's MediaWiki API (action=parse): (title, text, card), or None when the
    site has no such API. The page's infobox (an <aside> in Fandom's HTML) is kept: it holds the facts."""
    parts = urlsplit(url)
    m = WIKI_PAGE.match(parts.path)
    if not m:
        return None
    page = unquote(m.group(1))
    query = {"action": "parse", "page": page, "prop": "text|displaytitle", "format": "json", "formatversion": "2",
             "redirects": "1", "disableeditsection": "1"}
    for path in WIKI_APIS:
        api = f"{parts.scheme}://{parts.netloc}{path}?" + "&".join(f"{k}={quote(v, safe='|')}" for k, v in query.items())
        try:
            body, _, _, _ = _fetch(http, api, resolve, headers={"Accept": "application/json"})
            data = json.loads(body)
        except (ToolError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if (data.get("error") or {}).get("code") == "missingtitle":
            raise ToolError(f"The wiki has no page called '{page.replace('_', ' ')}'.", invalid=False, reason="not_found")
        html = str((data.get("parse") or {}).get("text") or "")
        if not html:
            continue
        html = html.replace("<aside", "<div").replace("</aside>", "</div>")
        _, text, _ = _html_text(f"<html><body><main>{html}</main></body></html>".encode("utf-8"), "utf-8", url)
        if not text.strip():
            continue
        title = data["parse"].get("title") or page.replace("_", " ")
        return title, text, {"site": _site(url), "favicon": urljoin(url, "/favicon.ico"), "via": "the wiki's API"}
    return None


def _read(http: httpx.Client, url: str, resolve) -> tuple[str | None, str, dict, str]:
    """(title, text, card, final url) of a page fetched as is."""
    body, ctype, charset, final = _fetch(http, url, resolve)
    meta = {"site": _site(final), "favicon": urljoin(final, "/favicon.ico")}
    kind = _link_kind(final, ctype, body)
    if kind in DOCUMENT_KINDS:
        title, text = _document_text(final, kind, body)
    elif kind in TABLE_SUFFIXES:
        title, text = _table_preview(final, kind, body, charset)
        meta["note"] = (f"This link is a data file ({kind.lstrip('.').upper()}), not a page: shown are its first rows. "
                        "To work with it, give its link in downloads: main brings it in as a table, with the user's "
                        "approval.")
    elif ctype.startswith("image/"):
        raise ToolError(f"This link is an image ({ctype}), not a page: it has no text to read.", invalid=False,
                        reason="unsupported")
    elif "html" in ctype or _looks_html(body):
        title, text, page_meta = _html_text(body, charset, final)
        meta.update(page_meta, _html=True)
        files = offered_files(body, final)
        if files:
            meta["files"] = files
        if b"<table" in body[:2_000_000].lower() and not _on_github(final):
            found = page_tables(body)
            if found:  # said with the read, so a table's numbers come in as data instead of copied (fetch_table)
                big = max(found, key=lambda t: t.size)
                meta["data_table"] = {"rows": len(big), "columns": [str(c) for c in big.columns][:8]}
        if len(text.split()) < 20 and meta.get("description"):  # content drawn by JavaScript
            text = (f"{meta['description']}\n\n(This page shows its content with JavaScript; only its description "
                    "could be read.)")
            meta["partial"] = True
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        title, text = None, body.decode(charset or "utf-8", "replace").strip()
    else:
        raise ToolError(f"The page is not text (it is {ctype or 'an unknown type'}).", invalid=False, reason="unsupported")
    return title, text, meta, final


GITHUB_API = "https://api.github.com/repos"
GITHUB_RAW = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28"}
GITHUB_JSON = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
GITHUB_NOT_OWNERS = {"about", "apps", "collections", "contact", "enterprise", "events", "explore", "features",
                     "issues", "login", "marketplace", "new", "notifications", "orgs", "organizations", "pricing",
                     "pulls", "search", "settings", "signup", "sponsors", "topics", "trending"}


def _from_github(http: httpx.Client, url: str, resolve) -> tuple[str, str, dict] | None:
    """(title, text, card) of a github.com repository page (README, with the repository's
    description, main language and license) or file page (/blob/<ref>/<path>, its raw text).
    None for any other address, or when GitHub's API can't answer - the page is then read as is."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if ((parts.hostname or "").lower() not in ("github.com", "www.github.com") or len(segs) < 2
            or segs[0].lower() in GITHUB_NOT_OWNERS or not all(GITHUB_NAME.fullmatch(s) for s in segs[:2])):
        return None
    owner, repo = segs[0], segs[1].removesuffix(".git")
    api = f"{GITHUB_API}/{owner}/{repo}"
    card = {"site": "github.com", "favicon": "https://github.com/favicon.ico"}

    def text_of(address: str, headers: dict) -> str:
        body = _fetch(http, address, resolve, headers=headers)[0]
        if b"\x00" in body[:4096]:
            raise ToolError("not a text file", invalid=False)
        return body.decode("utf-8", "replace").strip()

    if len(segs) >= 3 and segs[2] == "tree":  # a folder: /tree/<ref>/<path>, or the root at a ref
        ref, path = (segs[3], "/".join(segs[4:])) if len(segs) >= 4 else ("", "")
        try:
            listing = _github_files(json.loads(text_of(f"{api}/git/trees/{ref or 'HEAD'}?recursive=1", GITHUB_JSON)),
                                    path)
        except (ToolError, ValueError):
            return None
        where = f"{owner}/{repo}" + (f"/{path}" if path else "") + (f" ({ref})" if ref else "")
        return where, f"Files in {where}:\n{listing}", card
    if len(segs) == 2:
        try:
            info = json.loads(text_of(api, GITHUB_JSON))
        except (ToolError, ValueError):
            return None
        facts = [f"GitHub repository {info.get('full_name') or f'{owner}/{repo}'}."]
        license_ = (info.get("license") or {}).get("spdx_id")
        for label, value in (("Description", info.get("description")), ("Main language", info.get("language")),
                             ("License", license_ if license_ != "NOASSERTION" else None),
                             ("Homepage", info.get("homepage")), ("Stars", info.get("stargazers_count"))):
            if value not in (None, ""):
                facts.append(f"{label}: {value:,}." if isinstance(value, int) else f"{label}: {value}.")
        try:  # the real file list: a README describes files, it does not list them
            files = "Files:\n" + _github_files(json.loads(text_of(
                f"{api}/git/trees/{info.get('default_branch') or 'HEAD'}?recursive=1", GITHUB_JSON)))
        except (ToolError, ValueError):
            files = "(the file list could not be read)"
        try:
            readme = "README:\n" + text_of(f"{api}/readme", GITHUB_RAW)
        except ToolError:
            readme = "(no README)"
        if info.get("description"):
            card["description"] = info["description"][:300]
        return f"{owner}/{repo}", " ".join(facts) + "\n\n" + files + "\n\n" + readme, card
    if len(segs) >= 5 and segs[2] == "blob":
        ref, path = segs[3], "/".join(segs[4:])
        for address, headers in ((f"{api}/contents/{path}?ref={ref}", GITHUB_RAW),
                                 (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}", {})):
            try:
                return f"{owner}/{repo}: {path}", text_of(address, headers), card
            except ToolError:
                continue
    return None


# ---------------------------------------------------------------- downloads (import_table, download_file)

TABLE_SUFFIXES = filetypes.TABLE  # a .txt is a document (filetypes), not a table
TABLE_TYPES = {"text/csv": ".csv", "application/csv": ".csv", "text/tab-separated-values": ".tsv",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
               "application/vnd.ms-excel": ".xls"}
MAX_TABLE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
GITHUB_HOSTS = {"github.com", "www.github.com", "raw.githubusercontent.com", "media.githubusercontent.com",
                "gist.github.com", "gist.githubusercontent.com", "objects.githubusercontent.com",
                "release-assets.githubusercontent.com", "codeload.github.com"}
PEEK = 1024  # bytes read from a link to tell what it is, before the user approves the download


def look(url: str, *, github: bool = False, tables: bool = False, table: int | None = None,
         http: httpx.Client | None = None, resolve=None) -> dict:
    """What a link holds, for the user to approve before anything is downloaded: the file's name,
    type, size and the address to fetch it from (a GitHub file page gives its raw file; a file kept
    in Git LFS, its real one). Only the first bytes of the response are read. A web page instead of
    a file is a ToolError naming the files it links to. github: GitHub only (download_file);
    tables: a CSV, TSV or Excel file (import_table) - or, from a page off GitHub, the page's own data table (the
    largest, or number ``table``): a table on a page comes in as data, never retyped by a model (measured: a web
    helper copied a month of share prices out of a page into its answer, with closes missing and a high below its
    low, and the chart had nothing to draw from)."""
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        raise ToolError("Give the file's full http(s) link.", field="url")
    url = _raw_link(url)
    if github and not _on_github(url):
        raise ToolError("Only a file on GitHub (github.com, raw.githubusercontent.com, gists) is downloaded as it is; "
                        "from another site, files(mode='download') brings in a data table (CSV, Excel).", field="url")
    resolve = resolve or socket.getaddrinfo
    with _downloader(http) as client:
        head, ctype, size, final = _peek(client, url, resolve)
        if head.startswith(b"version https://git-lfs") and urlsplit(final).hostname == "raw.githubusercontent.com":
            # A file kept in Git LFS: raw.githubusercontent.com serves its pointer, GitHub's media host the file.
            stated = re.search(rb"\nsize (\d+)", head)
            final, ctype = "https://media.githubusercontent.com/media" + urlsplit(final).path, ""
            size = int(stated[1]) if stated else None
        if ctype in ("text/html", "application/xhtml+xml") or head.lstrip().lower().startswith((b"<!doctype", b"<html")):
            page = _fetch(client, url, resolve)[0]  # a page, read like read_webpage would: which files does it offer?
            if tables and not _on_github(final):
                found_tables = page_tables(page)
                if found_tables:
                    return _page_table(found_tables, table, final)
            found, total = _file_links(http, url, page, final, resolve, TABLE_SUFFIXES if tables else None)
            what = "data file" if tables else "file"
            if found:  # one call more instead of another search
                shown = (f"Files it has ({len(found)} of {total}; a folder's link lists that folder): "
                         if total > len(found) else "Files it has: ")  # measured: 10 shown were taken for all
                raise ToolError(f"That link is a web page, not a {what}. {shown}" + ", ".join(found)
                                + " - ask for one of these.", invalid=False)
            raise ToolError(f"That link is a web page, not a {what}" + (", and it shows no data table to a plain "
                            "read (it may draw one with JavaScript)" if tables else "") + ". A file's own link ends in "
                            "its name (e.g. .csv); ask research_web for it, or ask the user to upload the file.",
                            invalid=False)
    if github and not _on_github(final):
        raise ToolError("That link leads away from GitHub: only a file on GitHub is downloaded as it is.",
                        invalid=False)
    name = unquote(PurePosixPath(urlsplit(final).path).name) or "download"
    limit = MAX_TABLE_BYTES if tables else MAX_FILE_BYTES
    if size and size > limit:
        raise ToolError(f"The file is {size / 2 ** 20:.1f} MB; at most {limit // 2 ** 20} MB can be downloaded.",
                        invalid=False)
    kind = PurePosixPath(name).suffix.lower()
    # Data served as plain text counts (raw hosts serve CSV as text/plain) - not a document's own extension.
    if tables and kind not in TABLE_SUFFIXES and not (
            TABLE_TYPES.get(ctype) or (ctype.startswith("text/") and kind not in filetypes.DOCUMENT)):
        if kind in filetypes.DOCUMENT:
            raise ToolError(f"That link is a document ({kind}), not a data table. From outside GitHub only tables are "
                            "brought in: research_web reads a document, or the user uploads it.", invalid=False)
        raise ToolError(f"That link is not a CSV, TSV or Excel file (it is {ctype or 'of an unknown type'}). "
                        "files(mode='download') brings in a data table from any site, or any file from GitHub.",
                        invalid=False)
    return {"url": final, "name": name, "type": ctype, "bytes": size, "host": urlsplit(final).hostname,
            **({"github": True} if github else {})}


def fetch_table(ws, looked: dict, *, http: httpx.Client | None = None, resolve=None) -> ToolResult:
    """The approved data file (from look), downloaded and stored as a table - or a page's table, read again and
    stored as CSV."""
    body, ctype, final = _download(looked, MAX_TABLE_BYTES, http, resolve)
    if looked.get("table"):
        found = page_tables(body)
        if len(found) < looked["table"]:
            raise ToolError("The page changed since it was looked at: that table is no longer on it, so nothing was "
                            "saved.", invalid=False)
        body, ctype = found[looked["table"] - 1].to_csv(index=False).encode("utf-8"), "text/csv"
    suffix = PurePosixPath(urlsplit(final).path).suffix.lower()
    if looked.get("table") or suffix not in TABLE_SUFFIXES:
        suffix = TABLE_TYPES.get(ctype) or ".csv"
    name = looked["name"]
    name = name if name.lower().endswith(suffix) else name + suffix
    put = ws.put(name, body, kind="table", by="user", dedupe=True)  # the user approved it: their file
    data = put.key
    df = ws.table(data)
    preview = df.head(8).astype(object).where(df.head(8).notna(), None)
    saved = f"Already in the workspace as {data} (the same file)" if put.existed else f"Saved as {data}"
    others = (f" The page has {looked['tables']} data tables; this is number {looked['table']} - another comes in "
              "with files(mode='download', link=..., table=its number)." if looked.get("tables", 1) > 1 else "")
    return ok({"data": data, "rows": len(df), "columns": list(map(str, df.columns)),
               "source": final, "note": f"{saved}: give analyze_or_code a task on {data}.{others}"},
              {"type": "table", "title": put.path, "columns": list(map(str, df.columns)),
               "rows": preview.values.tolist(), "note": f"{len(df):,} rows from {urlsplit(final).hostname}"})


def fetch_file(ws, index, looked: dict, *, http: httpx.Client | None = None, resolve=None) -> ToolResult:
    """The approved GitHub file (from look), downloaded and kept like an upload: a data file as a
    table, a document indexed for search, anything else as a file."""
    from .files import store_file
    body, _, final = _download(looked, MAX_FILE_BYTES, http, resolve)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / Path(looked["name"]).name
        path.write_bytes(body)
        kept = store_file(ws, index, path)
    item = kept.get("id") or kept.get("file")
    use = {"table": f"give analyze_or_code a task on {item}", "document": "ask_knowledge can search it",
           "image": "view_image can look at it", "file": f"analyze_or_code's scripts can open it as '{item}'"}[kept["kind"]]
    saved = f"Already in the workspace as {item} (the same file)" if kept.get("same") else f"Saved as {item}"
    return ok({**kept, "bytes": len(body), "source": final, "note": f"{saved}: {use}."})


MIN_TABLE_ROWS = 3  # a page's table with fewer rows is layout or a key figure, not data
DATA_TABLE_NOTE = "This page holds a data table"  # read_webpage says it; the web check knows a page had one by it


def page_tables(body: bytes) -> list:
    """A page's data tables (pandas over lxml), in page order: at least MIN_TABLE_ROWS rows and two columns, the
    headers plain text (a header of two rows joined), empty rows out."""
    import io

    import pandas as pd
    try:
        found = pd.read_html(io.StringIO(body.decode("utf-8", "replace")))
    except (ValueError, ImportError):  # no <table> on the page
        return []
    out = []
    for t in found:
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [" ".join(dict.fromkeys(str(p) for p in c if not str(p).startswith("Unnamed")))
                         or f"column {i + 1}" for i, c in enumerate(t.columns)]
        else:
            t.columns = [f"column {i + 1}" if str(c).startswith("Unnamed") else str(c) for i, c in enumerate(t.columns)]
        t.columns = [_plain_header(c) for c in t.columns]
        t = t.dropna(how="all")
        if len(t) >= MIN_TABLE_ROWS and t.shape[1] >= 2:
            out.append(t)
    return out


def _plain_header(name: str) -> str:
    """A header with its tooltip read in too - 'Close Close price adjusted for splits.', 'Adj Close Adjusted close price
    adjusted for splits and dividend ...' (Yahoo's price history, measured 2026-10-17: the analyst's columns became
    close_close_price_adjusted_for_splits, one query failed on them) - is the header alone: 'Close', 'Adj Close'."""
    words = str(name).split()
    if len(words) < 5 or not str(name).rstrip().endswith("."):
        return str(name)
    for k in range(1, min(4, len(words) - 2)):
        if words[k][:1].isupper() and words[k].lower().startswith(words[0].lower()[:3]):
            return " ".join(words[:k])
    return str(name)


def _page_table(found: list, table: int | None, final: str) -> dict:
    """The page's table to bring in - the one asked for, else the largest - described for the user's approval."""
    if table is not None and not 1 <= table <= len(found):
        raise ToolError(f"That page has {len(found)} data table(s): table is 1 to {len(found)}.", field="table")
    i = table - 1 if table else max(range(len(found)), key=lambda k: found[k].size)
    host = urlsplit(final).hostname or "page"
    stem = re.sub(r"[^\w-]+", "-", PurePosixPath(urlsplit(final).path).name.rsplit(".", 1)[0] or host).strip("-")
    site = host.removeprefix("www.").split(".")[0]  # 'history.csv' says nothing: 'stockanalysis-history.csv'
    name = f"{site}-{stem or 'table'}{f'-table-{i + 1}' if len(found) > 1 else ''}.csv"
    t = found[i]
    return {"url": final, "name": name, "type": "text/html", "bytes": len(t.to_csv(index=False).encode("utf-8")),
            "host": host, "table": i + 1, "tables": len(found), "rows": len(t), "columns": [str(c) for c in t.columns][:12]}


def _download(looked: dict, limit: int, http, resolve) -> tuple[bytes, str, str]:
    with _downloader(http, timeout=60.0) as client:
        body, ctype, _, final = _fetch(client, looked["url"], resolve or socket.getaddrinfo, limit)
    if looked.get("github") and not _on_github(final):
        raise ToolError("The file's link led away from GitHub; nothing was saved.", invalid=False)
    return body, ctype, final


def _downloader(http: httpx.Client | None, timeout: float = 30.0):
    return (httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=False,
                         headers={"User-Agent": net.USER_AGENT}) if http is None else nullcontext(http))


def _peek(http: httpx.Client, url: str, resolve) -> tuple[bytes, str, int | None, str]:
    """(first bytes, content type, size or None, final url) - the redirects checked as in _fetch, the
    body not downloaded. Asked uncompressed, so the stated length is the file's size."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url, resolve)
        try:
            with http.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if not r.is_success:
                    why, reason = _why(r.status_code)
                    raise ToolError(f"The link answered HTTP {r.status_code}{why}.", invalid=False, reason=reason)
                head = b""
                for chunk in r.iter_bytes():
                    head += chunk
                    if len(head) >= PEEK:
                        break
                length = r.headers.get("content-length", "")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return head[:PEEK], ctype, int(length) if length.isdigit() else None, url
        except httpx.TimeoutException as exc:
            raise ToolError("The link did not answer in time.", invalid=False) from exc
        except httpx.HTTPError as exc:
            raise ToolError("The link could not be reached.", invalid=False) from exc
    raise ToolError(f"More than {MAX_REDIRECTS} redirects.", invalid=False)


def _raw_link(url: str) -> str:
    """A GitHub file page (github.com/<owner>/<repo>/blob/<ref>/<path>, or /raw/) as its raw file - and
    the half-converted raw.githubusercontent.com/<owner>/<repo>/blob/<ref>/<path> a model writes (measured
    twice, a 404 each time, once rewriting the link the user typed)."""
    parts, segs = urlsplit(url), [s for s in urlsplit(url).path.split("/") if s]
    host = (parts.hostname or "").lower()
    if host in ("github.com", "www.github.com", "raw.githubusercontent.com") and len(segs) >= 5 and segs[2] in (
            "blob", "raw"):
        return f"https://raw.githubusercontent.com/{segs[0]}/{segs[1]}/{'/'.join(segs[3:])}"
    return url


def _on_github(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in GITHUB_HOSTS


FILE_LINKS = 10


def _file_links(http: httpx.Client | None, url: str, body: bytes, final: str, resolve,
                suffixes: tuple[str, ...] | None) -> tuple[list[str], int]:
    """(the first FILE_LINKS links to files on a page, how many there are) - only those with these
    suffixes, if given: for a GitHub repository or folder, from its file tree (the page itself is drawn
    by JavaScript); otherwise the page's own links to such files."""
    parts, segs = urlsplit(url), [s for s in urlsplit(url).path.split("/") if s]
    if (parts.hostname or "").lower() in ("github.com", "www.github.com") and len(segs) >= 2 and (
            len(segs) == 2 or segs[2] == "tree"):
        owner, repo = segs[0], segs[1].removesuffix(".git")
        ref, under = (segs[3], "/".join(segs[4:])) if len(segs) >= 4 else ("HEAD", "")
        try:
            with _downloader(http, timeout=15.0) as c:
                tree = json.loads(_fetch(c, f"{GITHUB_API}/{owner}/{repo}/git/trees/{ref}?recursive=1", resolve,
                                         headers=GITHUB_JSON)[0])
        except (ToolError, ValueError):
            return [], 0
        paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"
                 and e.get("path", "").startswith(under) and (suffixes is None or (
                     PurePosixPath(e["path"]).suffix.lower() in suffixes and not e["path"].lower().endswith(".txt")))]
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{p}" for p in paths[:FILE_LINKS]], len(paths)
    from lxml import html as lxml_html
    try:
        doc = lxml_html.document_fromstring(body)
    except (ValueError, lxml_html.etree.ParserError):
        return [], 0
    wanted = suffixes or (".csv", ".tsv", ".xlsx", ".xls", ".json", ".txt", ".pdf", ".zip", ".py", ".ipynb")
    found = []
    for href in doc.xpath("//a/@href"):
        link = urljoin(final, str(href)).split("#")[0]
        if (PurePosixPath(urlsplit(link).path).suffix.lower() in wanted and link not in found
                and link.startswith(("http://", "https://"))):
            found.append(link)
    return found[:FILE_LINKS], len(found)


OFFERED = 8  # files a page lists in read_webpage's result


def offered_files(body: bytes, final: str) -> list[str]:
    """Links on a page to files main can download with the user's approval: data tables from any site,
    and files on GitHub (a file page or a raw file)."""
    from lxml import html as lxml_html
    try:
        doc = lxml_html.document_fromstring(body)
    except (ValueError, lxml_html.etree.ParserError):
        return []
    found = []
    for href in doc.xpath("//a/@href"):
        link = urljoin(final, str(href)).split("#")[0]
        if link.startswith(("http://", "https://")) and link not in found and is_file_link(link):
            found.append(link)
    return found[:OFFERED]


def is_file_link(link: str) -> bool:
    """A link files(mode='download') can fetch: a data table from any site, or a file on GitHub."""
    suffix = PurePosixPath(urlsplit(link).path).suffix.lower()
    if suffix in TABLE_SUFFIXES:
        return True
    return bool(_on_github(link) and suffix and (urlsplit(link).hostname != "github.com" or "/blob/" in link
                                                  or "/raw/" in link))


def page_age(published: str | None, updated: str | None, today: date | None = None) -> str | None:
    """'updated 2026-03-02, 7 months ago' - how old a page is, worked out by code: a 'latest' answer
    from a page years old is the known slip (the 'stale latest picks' of the user's chats)."""
    today = today or date.today()
    for word, value in (("updated", updated), ("published", published)):
        try:
            when = date.fromisoformat(str(value or "")[:10])
        except ValueError:
            continue
        days = (today - when).days
        if days < 0:
            return f"{word} {when}"
        span = ("today" if days == 0 else f"{days} days ago" if days < 60 else f"{days // 30} months ago"
                if days < 730 else f"{days // 365} years ago")
        return f"{word} {when}, {span}"
    return None


FILES_CHARS = 3_500  # the file list's share of a page's text


def _github_files(tree: dict, under: str = "") -> str:
    """A repository's files from GitHub's git tree, one line per folder ('src/: app.rs  bin/ ...'),
    shallow folders first, cut to FILES_CHARS. ``under``: only that folder and below."""
    if not isinstance(tree.get("tree"), list):
        raise ValueError("no tree")
    prefix = under.strip("/") + "/" if under.strip("/") else ""
    folders: dict[str, list[str]] = {}
    for entry in tree["tree"]:
        path = str(entry.get("path", ""))
        if prefix and not path.startswith(prefix):
            continue
        parent, _, name = path[len(prefix):].rpartition("/")
        folders.setdefault(parent, []).append(name + "/" if entry.get("type") == "tree" else name)
    if not folders:
        raise ValueError("no such folder")
    lines, shown = [], 0
    for folder in sorted(folders, key=lambda f: (f.count("/") + bool(f), f)):
        names = sorted(folders[folder], key=lambda n: (not n.endswith("/"), n.lower()))
        line = f"{prefix + folder + '/' if folder else prefix or '/'}: {'  '.join(names)}"
        if shown + len(line) > FILES_CHARS:
            lines.append("... more folders not shown; read one through its link: "
                         "github.com/<owner>/<repo>/tree/<branch>/<folder>")
            break
        lines.append(line)
        shown += len(line)
    if tree.get("truncated"):
        lines.append("(GitHub cut this list short: the repository is very large)")
    return "\n".join(lines)


YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def youtube_id(url: str) -> str | None:
    """The video id in a YouTube video link (watch?v=, youtu.be/, /shorts/, /live/, /embed/), else None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path.split("/")
    if host == "youtu.be":
        candidate = path[1] if len(path) > 1 else ""
    elif host in YOUTUBE_HOSTS and parts.path.rstrip("/") == "/watch":
        candidate = parse_qs(parts.query).get("v", [""])[0]
    elif host in YOUTUBE_HOSTS and len(path) > 2 and path[1] in ("shorts", "live", "embed"):
        candidate = path[2]
    else:
        return None
    return candidate if YOUTUBE_ID.fullmatch(candidate) else None


def _from_youtube(http: httpx.Client, url: str, resolve) -> tuple[str, str, dict] | None:
    """(title, text, card) of a YouTube video, or None when the address is not one."""
    vid = youtube_id(url)
    if not vid:
        return None
    watch = f"https://www.youtube.com/watch?v={vid}"
    try:
        info = _youtube_page(_fetch(http, watch, resolve, headers={"Accept-Language": "en"})[0])
    except ToolError:
        info = {}
    if not info.get("title"):  # e.g. a cookie-consent page instead of the video
        try:
            o = json.loads(_fetch(http, f"https://www.youtube.com/oembed?url={quote(watch, safe='')}&format=json",
                                  resolve)[0])
        except (ToolError, ValueError):
            raise ToolError("YouTube gave no details for this video (it may be private or removed).",
                            invalid=False) from None
        info = {"title": o.get("title"), "channel": o.get("author_name")}
    facts = [f"{label}: {info[key]}." for label, key in (("Channel", "channel"), ("Length", "length"),
                                                          ("Published", "published"), ("Views", "views"),
                                                          ("Category", "category")) if info.get(key)]
    text = "\n\n".join(filter(None, (f"YouTube video: {info['title']}", " ".join(facts),
                                     info.get("description") and "Description:\n" + info["description"],
                                     "What is said in the video can't be read; only the details above are available.")))
    card = {"site": "YouTube", "favicon": "https://www.youtube.com/favicon.ico",
            "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "author": info.get("channel"),
            "published": info.get("published"), "description": (info.get("description") or "")[:300] or None,
            "video": {"id": vid, "embed": f"https://www.youtube-nocookie.com/embed/{vid}", "length": info.get("length")}}
    return info["title"], text, {k: v for k, v in card.items() if v}


def _youtube_page(body: bytes) -> dict:
    """The details a watch page carries for search engines (schema.org microdata), plus the full
    description and view count from the page's own data."""
    from lxml import html as lxml_html

    try:
        doc = lxml_html.document_fromstring(body)
    except Exception:  # noqa: BLE001 - not a page we can read: the caller falls back to oEmbed
        return {}

    def first(xpath: str) -> str | None:
        found = doc.xpath(xpath)
        return " ".join(str(found[0]).split()) if found else None

    page = body.decode("utf-8", "replace")
    description = re.search(r'"shortDescription":("(?:[^"\\]|\\.)*")', page)
    views = re.search(r'"viewCount":"(\d+)"', page)
    length = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", first("//meta[@itemprop='duration']/@content") or "")
    h, m, s = (int(x or 0) for x in length.groups()) if length else (0, 0, 0)
    return {"title": first("//meta[@property='og:title']/@content"),
            "channel": first("//*[@itemprop='author']//*[@itemprop='name']/@content"),
            "length": (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}") if length and (h or m or s) else None,
            "published": (first("//meta[@itemprop='uploadDate']/@content") or "")[:10] or None,
            "views": f"{int(views.group(1)):,}" if views else None,
            "category": first("//meta[@itemprop='genre']/@content"),
            "description": json.loads(description.group(1)).strip() if description else
                           first("//meta[@property='og:description']/@content")}


def _focus(text: str, find: str) -> tuple[str, str]:
    """The start of a long page plus the lines about the words in ``find`` (with a line of context
    each side), in page order, within the size limit. Lines with more of the words come first, so
    'default port' finds the port line on a page where hundreds of lines say 'default'.
    By words only: focusing by meaning too was measured on 10 long pages with questions worded unlike the
    page (bench/web_focus.py, 2026-10-09) - no fact found that words missed (5 of 5 both ways), at 3-11 s
    more a read - so it was not kept."""
    words = [w for w in re.findall(r"\w+", find.lower()) if len(w) > 2] or find.lower().split()
    patterns = [re.compile(r"(?<!\w)" + re.escape(w)) for w in words]  # word starts: 'port' not in 'support'
    lines = text.splitlines()
    score = {i: sum(bool(p.search(line.lower())) for p in patterns) for i, line in enumerate(lines)}
    hits = sorted((i for i, s in score.items() if s), key=lambda i: (-score[i], i))
    head = text[:1500].rsplit("\n", 1)[0]
    keep, size = set(), len(head)
    for i in hits:
        block = [j for j in (i - 1, i, i + 1) if 0 <= j < len(lines) and j not in keep]
        extra = sum(len(lines[j]) + 1 for j in block)
        if size + extra > MAX_TEXT:
            break
        keep.update(block)
        size += extra
    parts, last = [head, "..."], -2
    for j in sorted(keep):
        if j != last + 1:
            parts.append("...")
        parts.append(lines[j])
        last = j
    found = f"the parts mentioning {', '.join(words)} ({len(hits)} lines)" if hits else f"nothing mentions {', '.join(words)}"
    return "\n".join(parts), f"The page is long: shown are its start and {found}."


def _fetch(http: httpx.Client, url: str, resolve, max_bytes: int = MAX_BYTES,
           headers: dict | None = None) -> tuple[bytes, str, str | None, str]:
    """(body, content type, charset or None, final url), following redirects one checked hop at a time."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url, resolve)
        try:
            with http.stream("GET", url, headers=headers) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if not r.is_success:
                    why, reason = _why(r.status_code)
                    raise ToolError(f"The page answered HTTP {r.status_code}{why}.", invalid=False, reason=reason)
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ToolError(f"The page is larger than {max_bytes // (1024 * 1024) or 1} MB.", invalid=False,
                                        reason="unsupported")
                    chunks.append(chunk)
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return b"".join(chunks), ctype, r.charset_encoding, url
        except httpx.TimeoutException as exc:
            raise ToolError("The page did not answer in time.", invalid=False, reason="unreachable") from exc
        except httpx.HTTPError as exc:
            raise ToolError("The page could not be reached.", invalid=False, reason="unreachable") from exc
    raise ToolError(f"More than {MAX_REDIRECTS} redirects.", invalid=False, reason="unreachable")


def _why(status: int) -> tuple[str, str | None]:
    """What a failed page means for the next step - so a model reads another source instead of
    searching again - and its reason code."""
    if status in (401, 403, 429):
        return " (the site refuses automated readers; read another source)", "blocked"
    if status in (404, 410):
        return " (the page does not exist)", "not_found"
    if status >= 500:
        return " (the site is not answering right now; read another source)", "unreachable"
    return "", None


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
            raise ToolError(f"The address {host} does not exist.", invalid=False, reason="not_found") from exc
        ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if any(not ip.is_global or ip.is_multicast for ip in ips):
        raise private


def _looks_html(body: bytes) -> bool:
    head = body[:2000].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<body" in head


STREAMED = re.compile(r"^S:\w+$")
INTO_BOUNDARY = re.compile(r"""\$RC\(\s*["'](B:\w+)["']\s*,\s*["'](S:\w+)["']""")  # $RC("B:1","S:1")
INTO_PLACEHOLDER = re.compile(r"""\$RS\(\s*["'](S:\w+)["']\s*,\s*["'](P:\w+)["']""")  # $RS("S:1","P:1")


def _place_streamed(doc) -> None:
    """The parts of a streamed page put where they belong. A site that streams its pages (React 18, so most
    Next.js sites) sends each part late, inside <div hidden id="S:n">, and a script moves it to its place as
    the page loads - into a placeholder (<template id="P:n">, $RS) or in place of a loading boundary's
    fallback (<template id="B:n">, $RC). Until then the content sits in a hidden element, which reading drops.
    Seen 2026-10-09: anime.com's article and allen.in's formulas read as nothing. Each part is shown and moved
    where the page's own script would put it (a boundary's fallback - a loading placeholder - removed, up to
    its end marker <!--/$-->); a part whose place isn't found stays where it is, its text still read."""
    from lxml import etree

    places = {}
    for script in doc.xpath("//script[not(@src)]"):
        text = script.text or ""
        places.update({s: b for b, s in INTO_BOUNDARY.findall(text)})
        places.update(dict(INTO_PLACEHOLDER.findall(text)))
    for part in doc.xpath("//div[@hidden][starts-with(@id, 'S:')]"):
        sid = part.get("id", "")
        if not STREAMED.match(sid):
            continue
        del part.attrib["hidden"]
        place_id = places.get(sid, "P:" + sid[2:])
        found = doc.xpath("//template[@id=$id]", id=place_id)
        if not found or found[0].getparent() is None:
            continue
        place = found[0]
        if place_id.startswith("B:"):  # the fallback goes: the siblings after the marker, up to <!--/$-->
            node = place.getnext()
            while node is not None and not (node.tag is etree.Comment and (node.text or "").strip() == "/$"):
                after = node.getnext()
                node.getparent().remove(node)
                node = after
            place.tail = None
        part.tail = place.tail
        place.getparent().replace(place, part)


def _html_text(body: bytes, charset: str | None, base: str = "") -> tuple[str | None, str, dict]:
    from lxml import html as lxml_html

    try:
        # Without a charset in the headers, lxml reads the page's own <meta charset>.
        doc = lxml_html.document_fromstring(body.decode(charset, "replace") if charset else body)
    except Exception:  # noqa: BLE001 - malformed beyond repair
        return None, "", {}
    meta = _page_meta(doc, base)
    title = " ".join((doc.findtext(".//title") or "").split()) or meta.pop("og_title", None)
    meta.pop("og_title", None)
    _place_streamed(doc)
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
        elif (tag in BLOCKS or "-" in tag) and not any(isinstance(c.tag, str) and (
                c.tag in BLOCKS | {"h1", "h2", "h3", "ul", "ol", "table"} or "-" in c.tag)
                                       for c in el):
            text = " ".join(el.text_content().split())
            if text and not (COOKIE_NOTICE.search(text) and len(text.split()) < 60):
                lines.append(("- " if tag == "li" else "") + text)
    text = "\n".join(dict.fromkeys(lines)).strip()  # dict: drops repeated lines (menus copied twice)
    if not text:
        text = " ".join(root.text_content().split())
    return title, text, meta


def _page_meta(doc, base: str) -> dict:
    """The card a UI shows for a page, from its <head>: Open Graph / Twitter / plain meta tags."""
    def meta(*names: str) -> str | None:
        for name in names:
            for el in doc.xpath(f"//meta[@property='{name}' or @name='{name}' or @itemprop='{name}']"):
                value = " ".join((el.get("content") or "").split())
                if value:
                    return value
        return None

    def absolute(href: str | None) -> str | None:
        if not href:
            return None
        url = urljoin(base, href.strip())
        return url if url.startswith(("http://", "https://")) else None

    icons = []
    for link in doc.xpath("//link[@rel and @href]"):
        rels = (link.get("rel") or "").lower().split()
        if "icon" in rels or "apple-touch-icon" in rels:
            icons.append(absolute(link.get("href")))
    times = [t.get("datetime") for t in doc.xpath("//time[@datetime]")]
    out = {"site": meta("og:site_name", "application-name"),
           "description": (meta("og:description", "description", "twitter:description") or "")[:300] or None,
           "image": absolute(meta("og:image", "og:image:url", "og:image:secure_url", "twitter:image")),
           "favicon": next((i for i in icons if i), None),
           "author": meta("author", "article:author", "parsely-author"),
           "published": meta("article:published_time", "og:published_time", "datePublished", "pubdate", "date",
                             "DC.date") or (times[0] if times else None),
           "updated": meta("article:modified_time", "og:updated_time", "dateModified", "last-modified"),
           "og_title": meta("og:title", "twitter:title"),
           "language": (doc.get("lang") or "").split("-")[0] or None}
    return {k: v for k, v in out.items() if v}


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


# ---------------------------------------------------------------- icons and preview images

ICON_BYTES = 256 * 1024
IMAGE_BYTES = 2 * 1024 * 1024
ICON_TTL = 24 * 3600
_icons: dict[str, tuple[float, tuple[bytes, str] | None]] = {}


def fetch_icon(page_url: str, favicon_url: str | None = None, *, http: httpx.Client | None = None,
               resolve=None) -> tuple[bytes, str] | None:
    """(bytes, mime) of a site's favicon - the page's own icon link, else /favicon.ico - or None.
    Cached per site for a day, failures included, so a page with 10 sources costs few requests."""
    origin = f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}"
    now = time.time()
    cached = _icons.get(origin)
    if cached and now - cached[0] < ICON_TTL:
        return cached[1]
    found = None
    for url in dict.fromkeys(u for u in (favicon_url, origin + "/favicon.ico") if u):
        found = _fetch_image(url, ICON_BYTES, http, resolve, allow_svg=True)
        if found:
            break
    if len(_icons) > 512:
        _icons.clear()
    _icons[origin] = (now, found)
    return found


def fetch_image(url: str, *, http: httpx.Client | None = None, resolve=None) -> tuple[bytes, str] | None:
    """(bytes, mime) of a page's preview image (PNG, JPEG, WebP or GIF, at most 2 MB), or None."""
    return _fetch_image(url, IMAGE_BYTES, http, resolve, allow_svg=False)


RASTER = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif", "ICO": "image/x-icon"}


def _fetch_image(url, max_bytes, http, resolve, allow_svg: bool) -> tuple[bytes, str] | None:
    from PIL import Image, UnidentifiedImageError
    own = http is None  # the API's icon and image requests: a client of their own, closed after
    http = http or httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=False,
                                headers={"User-Agent": net.USER_AGENT, "Accept": "image/*"})
    try:
        body, ctype, _, _ = _fetch(http, url, resolve or socket.getaddrinfo, max_bytes)
    except ToolError:
        return None
    finally:
        if own:
            http.close()
    if allow_svg and (ctype == "image/svg+xml" or body.lstrip()[:5].lower() in (b"<svg ", b"<?xml")):
        return (body, "image/svg+xml") if b"<svg" in body[:4096].lower() else None
    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None
    return (body, RASTER[fmt]) if fmt in RASTER else None


DOCUMENT_TYPES = {"application/pdf": ".pdf",
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                  "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}
DOCUMENT_KINDS = set(DOCUMENT_TYPES.values())
PREVIEW_ROWS = 20


def _link_kind(url: str, ctype: str, body: bytes) -> str | None:
    """What a link holds, by its content type, else its name (servers often say only octet-stream), else
    its first bytes: '.pdf', '.docx', '.pptx', '.csv', '.tsv', '.xlsx', '.xls' - or None (a page, text, an
    image ...)."""
    if body[:5] == b"%PDF-":
        return ".pdf"
    kind = DOCUMENT_TYPES.get(ctype) or TABLE_TYPES.get(ctype)
    if kind:
        return kind
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix in DOCUMENT_KINDS | {".xlsx", ".xls"} and ctype in ("", "application/octet-stream", "binary/octet-stream",
                                                                 "application/zip", "application/x-zip-compressed"):
        return suffix
    if suffix in (".csv", ".tsv") and not ("html" in ctype or _looks_html(body)):
        return suffix
    return None


def _document_text(url: str, kind: str, body: bytes) -> tuple[str | None, str]:
    """A PDF, Word or PowerPoint file read by the one document reader (the knowledge helper's and files(read)'s)."""
    from app.rag.extract import DocumentError, extract, lines
    name = PurePosixPath(urlsplit(url).path).name or "document"
    try:
        doc = extract(name if name.lower().endswith(kind) else f"{name}{kind}", body)
    except DocumentError as exc:
        raise ToolError(f"The document could not be read: {exc}", invalid=False, reason="unsupported") from exc
    return doc.title, "\n".join(lines(doc))


def _table_preview(url: str, kind: str, body: bytes, charset: str | None) -> tuple[str | None, str]:
    """A data file's first rows and its columns - what it holds, so the helper can say whether it is the one."""
    import pandas as pd
    name = PurePosixPath(urlsplit(url).path).name or f"data{kind}"
    try:
        if kind in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(body), nrows=PREVIEW_ROWS)
        else:
            df = pd.read_csv(io.BytesIO(body), sep="\t" if kind == ".tsv" else None, engine="python", nrows=PREVIEW_ROWS,
                             encoding=charset or "utf-8", encoding_errors="replace", on_bad_lines="skip")
    except Exception as exc:  # noqa: BLE001 - pandas raises many kinds for a file that isn't what it says
        raise ToolError(f"This link is a data file ({kind.lstrip('.').upper()}) that could not be read as a table.",
                        invalid=False, reason="unsupported") from exc
    columns = ", ".join(map(str, df.columns))
    return name, f"Columns ({len(df.columns)}): {columns}\n\nFirst {len(df)} rows:\n{df.to_csv(index=False)}"
