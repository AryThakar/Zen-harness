"""web_search (fake engine) and read_webpage (fake server and DNS) - no network."""

from __future__ import annotations

import socket

import httpx
import pytest

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
