"""web_search (fake engine) and read_webpage (fake server and DNS) - no network."""

from __future__ import annotations

import socket

import httpx
import pytest

from app.tools import web
from app.tools.web import read_webpage, web_search

# ---------------------------------------------------------------- web_search


def engine(*answers):
    """A fake search engine: one answer (list of results, or an exception) per tier asked."""
    asked = []

    def search(query, k, tier, timeout):
        asked.append((query, tier))
        answer = answers[len(asked) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer
    search.asked = asked
    return search


HITS = [{"title": "Python 3.14 released", "href": "https://www.python.org/downloads/release/python-3140/",
         "body": "Python 3.14.0 is the newest major release."},
        {"title": "What's new", "href": "https://docs.python.org/3/whatsnew/3.14.html", "body": "Summary  of\nchanges."},
        {"title": "Dup", "href": "https://www.python.org/downloads/release/python-3140/", "body": "again"},
        {"title": "Elsewhere", "href": "https://example.com/python", "body": "unrelated site"}]


def test_results_are_cleaned_and_deduplicated():
    e = engine(HITS)
    r = web_search("python 3.14 release", engine=e)
    assert r.status == "ok" and len(r.result["results"]) == 3
    assert r.result["results"][1]["snippet"] == "Summary of changes."
    assert "read_webpage" in r.result["note"] and len(e.asked) == 1


def test_second_tier_only_when_the_first_finds_nothing():
    class DDGSException(Exception):
        pass
    e = engine(DDGSException("No results found."), HITS[:1])
    r = web_search("rare query", engine=e)
    assert [t for _, t in e.asked] == [e.asked[0][1], e.asked[1][1]] and len(e.asked) == 2
    assert r.result["results"][0]["title"] == "Python 3.14 released"
    r = web_search("nothing at all", engine=engine([], []))
    assert r.status == "ok" and r.result["results"] == [] and "No results" in r.result["note"]


def test_rate_limit_is_a_clear_failure():
    class RatelimitException(Exception):
        pass
    r = web_search("anything", engine=engine(RatelimitException("202 Ratelimit"), RatelimitException("x")))
    assert r.status == "failed" and "rate limiting" in r.result["error"]["message"]


def test_site_restricts_results():
    e = engine(HITS)
    r = web_search("whatsnew", site="https://www.python.org/", engine=e)
    assert e.asked[0][0] == "whatsnew site:python.org"
    assert {x["url"].split("/")[2] for x in r.result["results"]} == {"www.python.org", "docs.python.org"}
    assert web_search("x y", site="not a domain", engine=e).status == "invalid_arguments"


# ---------------------------------------------------------------- read_webpage

PUBLIC = {"news.example.com": "93.184.216.34", "docs.example.com": "93.184.216.35",
          "sneaky.example.com": "10.0.0.5", "rebind.example.com": "127.0.0.1"}


def resolve(host, port, proto=0):
    if host not in PUBLIC:
        try:
            socket.inet_aton(host)
            ip = host
        except OSError:
            raise socket.gaierror("no such host") from None
    else:
        ip = PUBLIC[host]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


ARTICLE = b"""<!doctype html><html><head><title>Rates rise again</title><script>var x = 'tracker';</script></head>
<body><nav><a href="/">Home</a> | <a href="/world">World</a></nav>
<header>Daily News - subscribe!</header>
<article><h1>Rates rise again</h1><p>The central bank raised rates by 0.25 points on Tuesday.</p>
<p>Markets had expected the move.</p><ul><li>Inflation: 3.1%</li><li>Next meeting: March</li></ul></article>
<aside>Related: celebrity gossip</aside><footer>(c) Daily News</footer>
<div hidden>secret tracking text</div></body></html>"""


def site(routes):
    def handler(request):
        answer = routes.get(str(request.url))
        if answer is None:
            return httpx.Response(404)
        return answer() if callable(answer) else answer
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_article_text_without_page_furniture():
    http = site({"https://news.example.com/rates": httpx.Response(200, content=ARTICLE,
                                                                   headers={"content-type": "text/html; charset=utf-8"})})
    r = read_webpage("news.example.com/rates", http=http, resolve=resolve)  # https:// is added
    assert r.status == "ok" and r.result["title"] == "Rates rise again"
    text = r.result["text"]
    assert text.startswith("# Rates rise again") and "raised rates by 0.25 points" in text and "- Inflation: 3.1%" in text
    for junk in ("tracker", "Home", "subscribe", "gossip", "(c) Daily News", "secret"):
        assert junk not in text


def test_private_addresses_are_refused_even_after_a_redirect():
    http = site({"https://news.example.com/go": httpx.Response(302, headers={"location": "http://127.0.0.1:8080/admin"}),
                 "https://news.example.com/go2": httpx.Response(301, headers={"location": "https://sneaky.example.com/"})})
    for url in ["https://news.example.com/go", "https://news.example.com/go2", "http://localhost:8000/api",
                "http://192.168.1.1/", "https://rebind.example.com/", "http://[::1]/"]:
        r = read_webpage(url, http=http, resolve=resolve)
        assert r.status == "invalid_arguments" and "private network" in r.result["error"]["message"], url
    assert read_webpage("ftp://news.example.com/file", http=http, resolve=resolve).status == "invalid_arguments"


def test_redirects_between_public_pages_are_followed():
    http = site({"https://news.example.com/old": httpx.Response(301, headers={"location": "/new"}),
                 "https://news.example.com/new": httpx.Response(200, text="Plain text page.",
                                                                headers={"content-type": "text/plain"})})
    r = read_webpage("https://news.example.com/old", http=http, resolve=resolve)
    assert r.result["url"] == "https://news.example.com/new" and r.result["text"] == "Plain text page."


def test_large_binary_missing_and_empty_pages_fail_clearly():
    http = site({"https://docs.example.com/big": lambda: httpx.Response(200, content=b"a" * (3 * 1024 * 1024 + 10),
                                                                         headers={"content-type": "text/plain"}),
                 "https://docs.example.com/img": httpx.Response(200, content=b"\x89PNG....", headers={"content-type": "image/png"}),
                 "https://docs.example.com/app": httpx.Response(200, content=b"<html><body><div id=app></div><script>x</script></body></html>",
                                                                headers={"content-type": "text/html"})})
    assert "larger than 3 MB" in read_webpage("https://docs.example.com/big", http=http, resolve=resolve).result["error"]["message"]
    assert "not text" in read_webpage("https://docs.example.com/img", http=http, resolve=resolve).result["error"]["message"]
    assert "JavaScript" in read_webpage("https://docs.example.com/app", http=http, resolve=resolve).result["error"]["message"]
    r = read_webpage("https://docs.example.com/missing", http=http, resolve=resolve)
    assert r.status == "failed" and "404" in r.result["error"]["message"]
    assert read_webpage("https://nowhere.example.org/", http=http, resolve=resolve).status == "failed"


@pytest.mark.parametrize("charset_header", [True, False])
def test_page_encoding_is_respected(charset_header):
    page = "<html><head><meta charset='windows-1252'><title>Café</title></head><body><p>Crème brûlée</p></body></html>"
    headers = {"content-type": "text/html; charset=windows-1252" if charset_header else "text/html"}
    http = site({"https://news.example.com/menu": httpx.Response(200, content=page.encode("cp1252"), headers=headers)})
    r = read_webpage("https://news.example.com/menu", http=http, resolve=resolve)
    assert r.result["title"] == "Café" and "Crème brûlée" in r.result["text"]


# ---------------------------------------------------------------- metadata for the UI

CARD_PAGE = b"""<html lang="en-GB"><head><title>Rates rise | Daily News</title>
<meta property="og:site_name" content="Daily News"><meta property="og:image" content="/img/rates.jpg">
<meta name="description" content="The central bank raised rates again."><meta name="author" content="A. Writer">
<meta property="article:published_time" content="2026-09-30T08:00:00Z">
<link rel="shortcut icon" href="/static/icon.png"></head><body><article><p>Rates went up.</p></article></body></html>"""


def png(size=(16, 16)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return buf.getvalue()


def test_page_card_comes_from_the_head():
    http = site({"https://news.example.com/rates": httpx.Response(200, content=CARD_PAGE,
                                                                   headers={"content-type": "text/html"})})
    r = read_webpage("https://news.example.com/rates", http=http, resolve=resolve)
    card = r.artifact
    assert card["site"] == "Daily News" and card["image"] == "https://news.example.com/img/rates.jpg"
    assert card["favicon"] == "https://news.example.com/static/icon.png" and card["language"] == "en"
    assert card["published"] == "2026-09-30T08:00:00Z" and card["author"] == "A. Writer"
    assert card["description"] == "The central bank raised rates again."
    assert r.result["published"] == "2026-09-30T08:00:00Z" and "image" not in r.result  # the model gets the essentials


def test_search_results_carry_site_and_favicon():
    r = web_search("x y", engine=engine(HITS[:1]))
    assert r.result["results"][0]["site"] == "python.org"
    assert r.result["results"][0]["favicon"] == "https://www.python.org/favicon.ico"


def test_icons_are_fetched_checked_and_cached():
    from app.tools import web
    web._icons.clear()
    calls = []
    http = site({"https://news.example.com/static/icon.png": lambda: (calls.append(1), httpx.Response(200, content=png()))[1],
                 "https://docs.example.com/favicon.ico": httpx.Response(200, content=b"<html>not an icon</html>")})
    icon = web.fetch_icon("https://news.example.com/rates", "https://news.example.com/static/icon.png", http=http, resolve=resolve)
    assert icon and icon[1] == "image/png"
    assert web.fetch_icon("https://news.example.com/other", None, http=http, resolve=resolve) == icon and len(calls) == 1
    assert web.fetch_icon("https://docs.example.com/page", None, http=http, resolve=resolve) is None  # HTML is not an icon
    # an icon link pointing into a private network is never fetched
    assert web.fetch_icon("https://sneaky.example.com/", "http://127.0.0.1/icon.png", http=http, resolve=resolve) is None


def test_preview_images_must_be_real_raster_images():
    from app.tools import web
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    http = site({"https://news.example.com/a.png": httpx.Response(200, content=png((64, 32))),
                 "https://news.example.com/b.svg": httpx.Response(200, content=svg, headers={"content-type": "image/svg+xml"})})
    assert web.fetch_image("https://news.example.com/a.png", http=http, resolve=resolve)[1] == "image/png"
    assert web.fetch_image("https://news.example.com/b.svg", http=http, resolve=resolve) is None


def test_long_pages_can_be_searched_with_find():
    filler = "\n".join(f"<p>Option number {i} does something unrelated to networking at all.</p>" for i in range(400))
    page = f"<html><body><main><h1>Server</h1><p>Intro.</p>{filler}<p>--port N: port to listen (default: 8080)</p></main></body></html>"
    http = site({"https://docs.example.com/server": lambda: httpx.Response(200, content=page.encode(),
                                                                            headers={"content-type": "text/html"})})
    cut = read_webpage("https://docs.example.com/server", http=http, resolve=resolve).result
    assert "8080" not in cut["text"] and "find=" in cut["truncated"]
    found = read_webpage("https://docs.example.com/server", "default port", http=http, resolve=resolve).result
    assert "default: 8080" in found["text"] and found["text"].startswith("# Server") and "parts mentioning" in found["shown"]


def test_find_prefers_lines_with_more_of_the_words():
    noise = "\n".join(f"<p>--opt-{i} N: default value {i} (support for imports)</p>" for i in range(600))
    page = f"<html><body><main><h1>Server</h1>{noise}<p>--port N: port to listen (default: 8080)</p></main></body></html>"
    http = site({"https://docs.example.com/server": lambda: httpx.Response(200, content=page.encode(),
                                                                            headers={"content-type": "text/html"})})
    found = read_webpage("https://docs.example.com/server", "default port", http=http, resolve=resolve).result
    assert "default: 8080" in found["text"]  # one line with both words beats 600 with one ('support' isn't 'port')


def test_failed_pages_say_what_to_do_next():
    http = site({"https://docs.example.com/busy": httpx.Response(503)})
    message = read_webpage("https://docs.example.com/busy", http=http, resolve=resolve).result["error"]["message"]
    assert "503" in message and "read another source" in message


SERVICES = {"github.com": "140.82.112.4", "api.github.com": "140.82.112.6", "raw.githubusercontent.com": "185.199.108.133",
            "www.youtube.com": "142.250.183.14"}


def services_resolve(host, port, proto=0):
    if host in SERVICES:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (SERVICES[host], port))]
    return resolve(host, port, proto)


def test_github_pages_are_read_through_its_api():
    repo = {"full_name": "acme/db", "description": "A fast database.", "language": "C++",
            "license": {"spdx_id": "MIT"}, "stargazers_count": 1234}
    issue = b"<html><head><title>Crash on start</title></head><body><main><p>Crash on start when the port is taken.</p></main></body></html>"
    routes = {"https://api.github.com/repos/acme/db": httpx.Response(200, json=repo),
              "https://api.github.com/repos/acme/db/readme": httpx.Response(200, text="# db\nRun it on port 9000."),
              "https://api.github.com/repos/acme/db/contents/src/server.py?ref=main": httpx.Response(403, text="rate limit"),
              "https://raw.githubusercontent.com/acme/db/main/src/server.py": httpx.Response(200, text="PORT = 9000\n"),
              "https://github.com/acme/db/issues/7": httpx.Response(200, content=issue, headers={"content-type": "text/html"})}
    asked = []

    def handler(request):
        asked.append((str(request.url), request.headers.get("accept")))
        return routes.get(str(request.url), httpx.Response(404))
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    r = read_webpage("https://github.com/acme/db", http=http, resolve=services_resolve)
    assert r.status == "ok" and r.result["url"] == "https://github.com/acme/db" and r.result["title"] == "acme/db"
    assert "Main language: C++. License: MIT." in r.result["text"] and "Stars: 1,234." in r.result["text"]
    assert "port 9000" in r.result["text"] and r.artifact["description"] == "A fast database."
    assert ("https://api.github.com/repos/acme/db/readme", "application/vnd.github.raw+json") in asked
    f = read_webpage("https://github.com/acme/db/blob/main/src/server.py", http=http, resolve=services_resolve)
    assert f.result["text"] == "PORT = 9000" and f.result["title"] == "acme/db: src/server.py"  # API refused -> raw file
    other = read_webpage("https://github.com/acme/db/issues/7", http=http, resolve=services_resolve)
    assert other.status == "ok" and "port is taken" in other.result["text"]  # other GitHub pages: read as HTML


WATCH = r"""<html><head><meta property="og:title" content="How DuckDB Works">
<meta property="og:description" content="Short version."></head><body>
<div itemscope itemtype="http://schema.org/VideoObject"><meta itemprop="duration" content="PT1H2M5S">
<meta itemprop="uploadDate" content="2024-05-01T08:00:00-07:00"><meta itemprop="genre" content="Education">
<span itemprop="author" itemscope itemtype="http://schema.org/Person"><link itemprop="name" content="Data Talks"></span></div>
<script>var ytInitialPlayerResponse = {"videoDetails":{"viewCount":"12345","shortDescription":"Chapters:\n0:00 Intro\n5:10 \"Vectors\""}};</script>
<footer>(c) 2026 Google LLC</footer></body></html>"""


def test_youtube_videos_are_read_from_their_watch_page():
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if request.url.path == "/watch" and request.url.params["v"] == "abcDEF12345":
            return httpx.Response(200, text=WATCH, headers={"content-type": "text/html"})
        if request.url.path == "/watch":  # a consent page instead of the video
            return httpx.Response(200, text="<html><body><p>Before you continue</p></body></html>")
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "A short", "author_name": "Someone"})
        return httpx.Response(404)
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    r = read_webpage("https://youtu.be/abcDEF12345", http=http, resolve=services_resolve)
    text = r.result["text"]
    assert r.status == "ok" and r.result["title"] == "How DuckDB Works" and r.result["author"] == "Data Talks"
    assert "Length: 1:02:05. Published: 2024-05-01. Views: 12,345. Category: Education." in text
    assert '5:10 "Vectors"' in text and "can't be read" in text and "Google LLC" not in text
    assert r.artifact["video"] == {"id": "abcDEF12345", "embed": "https://www.youtube-nocookie.com/embed/abcDEF12345",
                                   "length": "1:02:05"}
    assert r.artifact["image"] == "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg"
    s = read_webpage("https://www.youtube.com/shorts/zzzZZZ00000", http=http, resolve=services_resolve)
    assert s.result["title"] == "A short" and "Channel: Someone." in s.result["text"]  # oEmbed fallback
    assert web.youtube_id("https://www.youtube.com/@channel") is None
    assert web.youtube_id("https://m.youtube.com/watch?v=abcDEF12345&t=30") == "abcDEF12345"


def test_video_search_results_carry_a_thumbnail_for_the_ui():
    hits = [{"title": "Talk", "href": "https://www.youtube.com/watch?v=abcDEF12345", "body": "a talk"}, HITS[0]]
    r = web_search("duckdb talk", engine=engine(hits))
    assert r.artifact["items"][0]["image"] == "https://i.ytimg.com/vi/abcDEF12345/hqdefault.jpg"
    assert "image" not in r.result["results"][0] and "image" not in r.artifact["items"][1]  # the model doesn't need it


def test_pages_drawn_by_javascript_give_their_description():
    page = b'<html><head><meta name="description" content="Fast analytics for everyone."></head><body><div id=app></div></body></html>'
    http = site({"https://docs.example.com/spa": httpx.Response(200, content=page, headers={"content-type": "text/html"})})
    r = read_webpage("https://docs.example.com/spa", http=http, resolve=resolve)
    assert r.status == "ok" and r.result["text"].startswith("Fast analytics for everyone.") and "JavaScript" in r.result["text"]
