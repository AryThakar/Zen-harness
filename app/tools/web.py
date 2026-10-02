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
              GitHub pages are read through GitHub's API instead (a repository's README plus its
              description, main language and license; a file's raw text): the HTML pages are mostly
              interface and answer 503 now and then. Without a token the API allows 60 calls an
              hour; past that a file comes from raw.githubusercontent.com and a repository page is
              read as HTML.
              YouTube videos are read from their public watch page: title, channel, length, date,
              views, category and the full description (YouTube's oEmbed - title and channel - when
              the page doesn't have them). What is said in a video is not available: captions are
              only served to YouTube's own player, which proves it is a real app.
              For the UI, each page also yields its card: title, site name, description, preview
              image (og:image), favicon, author and published date, read from the page's <head>.
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
import time
from urllib.parse import parse_qs, quote, urljoin, urlsplit

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
    items = [{**r, "image": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg", "video": v} if (v := youtube_id(r["url"])) else r
             for r in results]  # video results show their thumbnail in the UI
    return ok(out, {"type": "links", "query": query, "items": items})


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
                                "snippet": " ".join(str(r.get("body") or r.get("snippet") or "").split())[:400],
                                "site": _site(url), "favicon": urljoin(url, "/favicon.ico")})
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
def read_webpage(url: str, find: str | None = None, *, http: httpx.Client | None = None,
                 resolve=None) -> ToolResult:
    """The readable text of one public web page (HTML, plain text or PDF). A page longer than 12,000
    characters is cut; with ``find`` the parts about those words are shown instead."""
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            raise ToolError("Only http and https addresses can be read.", field="url")
        url = "https://" + url
    if len(url) > 2048:
        raise ToolError("The address is too long.", field="url")
    http = http or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False,
                                headers={"User-Agent": net.USER_AGENT, "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5"})
    resolve = resolve or socket.getaddrinfo
    special = _from_github(http, url, resolve) or _from_youtube(http, url, resolve)
    if special:
        title, text, meta = special
        final = url
    else:
        title, text, meta, final = _read(http, url, resolve)
    if not text.strip():
        raise ToolError("No readable text was found on the page (it may need JavaScript to show its content).",
                        invalid=False)
    truncated = len(text) > MAX_TEXT
    shown = None
    if truncated and find and find.strip():
        text, shown = _focus(text, find)
    elif truncated:
        text = text[:MAX_TEXT].rsplit(" ", 1)[0] + " ..."
    result = {"url": final, "title": title, "site": meta["site"],
              **{k: meta[k] for k in ("published", "author") if meta.get(k)},
              "text": text, "words": len(text.split()), "note": "Quote only what this page says and cite its URL."}
    if shown:
        result["shown"] = shown
    elif truncated:
        result["truncated"] = (f"The page is long: only the first {MAX_TEXT:,} characters are shown. If what you need "
                               "is not here, call read_webpage again with find='a few words to look for'.")
    return ok(result, {"type": "webpage", "url": final, "title": title or meta["site"], **meta})


def _read(http: httpx.Client, url: str, resolve) -> tuple[str | None, str, dict, str]:
    """(title, text, card, final url) of a page fetched as is."""
    body, ctype, charset, final = _fetch(http, url, resolve)
    meta = {"site": _site(final), "favicon": urljoin(final, "/favicon.ico")}
    if ctype == "application/pdf" or body[:5] == b"%PDF-":
        title, text = _pdf_text(body)
    elif "html" in ctype or _looks_html(body):
        title, text, page_meta = _html_text(body, charset, final)
        meta.update(page_meta)
        if len(text.split()) < 20 and meta.get("description"):  # content drawn by JavaScript
            text = (f"{meta['description']}\n\n(This page shows its content with JavaScript; only its description "
                    "could be read.)")
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        title, text = None, body.decode(charset or "utf-8", "replace").strip()
    else:
        raise ToolError(f"The page is not text (it is {ctype or 'an unknown type'}).", invalid=False)
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
        try:
            readme = "README:\n" + text_of(f"{api}/readme", GITHUB_RAW)
        except ToolError:
            readme = "(no README)"
        if info.get("description"):
            card["description"] = info["description"][:300]
        return f"{owner}/{repo}", " ".join(facts) + "\n\n" + readme, card
    if len(segs) >= 5 and segs[2] == "blob":
        ref, path = segs[3], "/".join(segs[4:])
        for address, headers in ((f"{api}/contents/{path}?ref={ref}", GITHUB_RAW),
                                 (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}", {})):
            try:
                return f"{owner}/{repo}: {path}", text_of(address, headers), card
            except ToolError:
                continue
    return None


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
    'default port' finds the port line on a page where hundreds of lines say 'default'."""
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
                    raise ToolError(f"The page answered HTTP {r.status_code}{_why(r.status_code)}.", invalid=False)
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ToolError(f"The page is larger than {max_bytes // (1024 * 1024) or 1} MB.", invalid=False)
                    chunks.append(chunk)
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return b"".join(chunks), ctype, r.charset_encoding, url
        except httpx.TimeoutException as exc:
            raise ToolError("The page did not answer in time.", invalid=False) from exc
        except httpx.HTTPError as exc:
            raise ToolError("The page could not be reached.", invalid=False) from exc
    raise ToolError(f"More than {MAX_REDIRECTS} redirects.", invalid=False)


def _why(status: int) -> str:
    """What a failed page means for the next step - so a model reads another source instead of
    searching again."""
    if status in (401, 403, 429):
        return " (the site refuses automated readers; read another source)"
    if status in (404, 410):
        return " (the page does not exist)"
    if status >= 500:
        return " (the site is not answering right now; read another source)"
    return ""


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
    http = http or httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=False,
                                headers={"User-Agent": net.USER_AGENT, "Accept": "image/*"})
    try:
        body, ctype, _, _ = _fetch(http, url, resolve or socket.getaddrinfo, max_bytes)
    except ToolError:
        return None
    if allow_svg and (ctype == "image/svg+xml" or body.lstrip()[:5].lower() in (b"<svg ", b"<?xml")):
        return (body, "image/svg+xml") if b"<svg" in body[:4096].lower() else None
    try:
        with Image.open(io.BytesIO(body)) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None
    return (body, RASTER[fmt]) if fmt in RASTER else None


def _pdf_text(body: bytes) -> tuple[str | None, str]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [p.extract_text() or "" for p in reader.pages[:30]]
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for broken files
        raise ToolError("The PDF could not be read.", invalid=False) from exc
    title = (reader.metadata or {}).get("/Title") if reader.metadata else None
    return (str(title) if title else None), "\n\n".join(f"[page {i + 1}]\n{t.strip()}" for i, t in enumerate(pages) if t.strip())
