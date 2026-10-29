"""Hermetic tests for websearch-mcp.

No network and no API keys: DNS, HTTP (httpx.MockTransport) and the cache file
are stubbed, so the suite runs offline and in CI.
"""

import asyncio
import socket
import sqlite3
import time

import httpx
import pytest

import server


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setattr(server, "ALLOW_PRIVATE_HOSTS", False)
    return tmp_path


# --- helpers ---------------------------------------------------------------


def _resolver(mapping):
    """Fake socket.getaddrinfo: host -> ip; unknown hosts raise gaierror."""

    def fake(host, port, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror("nodename nor servname provided, or not known")
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return fake


def _public_resolver():
    return _resolver({"example.com": "93.184.216.34"})


def _mock_client(handler, headers=None):
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        headers=headers or {},
    )


# --- cache -----------------------------------------------------------------


def test_cache_put_get_roundtrip():
    server._cache_put("k", "value")
    assert server._cache_get("k", 60) == "value\n[local cache, 0h old]"


def test_cache_expired_entry_is_a_miss():
    server._cache_put("k", "value")
    with sqlite3.connect(server._DB) as con:
        con.execute("UPDATE cache SET ts=? WHERE key='k'", (time.time() - 7200,))
    assert server._cache_get("k", 3600) is None


def test_cache_survives_a_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB", str(tmp_path / "nested" / "cache.sqlite3"))
    assert server._cache_get("k", 60) is None
    server._cache_put("k", "value")  # must not raise


def test_ckey_is_stable_and_argument_sensitive():
    assert server._ckey("a", 1) == server._ckey("a", 1)
    assert server._ckey("a", 1) != server._ckey("a", 2)


# --- clamping --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(0, 1), (1, 1), (5, 5), (20, 20), (21, 20), ("7", 7), ("junk", 1), (None, 1)],
)
def test_clamp(raw, expected):
    assert server._clamp(raw, 1, server.MAX_RESULTS) == expected


# --- tool annotations --------------------------------------------------------


def test_every_tool_declares_the_full_read_only_hint_set():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

    assert set(tools) == {
        "search",
        "search_news",
        "search_semantic",
        "search_github",
        "fetch",
    }
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, f"{name} declares no annotations"
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is False, name
        assert ann.idempotentHint is True, name
        assert ann.openWorldHint is True, name


# --- SSRF guard ------------------------------------------------------------


def test_host_check_allows_public_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())
    assert server._host_check("example.com") == (True, "")


@pytest.mark.parametrize(
    "host,ip",
    [
        ("loop.example", "127.0.0.1"),
        ("private.example", "10.1.2.3"),
        ("metadata.example", "169.254.169.254"),
        ("v6loop.example", "::1"),
    ],
)
def test_host_check_rejects_non_public(monkeypatch, host, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({host: ip}))
    ok, reason = server._host_check(host)
    assert ok is False
    assert "non-public address" in reason and ip in reason


def test_host_check_reports_unresolvable_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({}))
    ok, reason = server._host_check("nope.invalid")
    assert ok is False
    assert "did not resolve" in reason


# --- fetch: redirects and caps --------------------------------------------


def test_fetch_refuses_redirect_to_loopback_before_sending_it(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _resolver({"example.com": "93.184.216.34", "127.0.0.1": "127.0.0.1"}),
    )

    out = server.fetch("https://example.com/page")

    assert seen == ["https://example.com/page"], "the private hop must never be requested"
    assert out.startswith("fetch failed: refusing to fetch")
    assert "non-public address" in out


def test_fetch_refuses_private_initial_url(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x" * 300)

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"127.0.0.1": "127.0.0.1"}))

    out = server.fetch("http://127.0.0.1/admin")

    assert seen == []
    assert out.startswith("fetch failed: refusing to fetch")


def test_fetch_follows_relative_redirects(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"y" * 400
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    out = server.fetch("https://example.com/a")

    assert seen == ["https://example.com/a", "https://example.com/b"]
    assert out == "y" * 400


def test_fetch_stops_on_redirect_loop(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "/a"})

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    out = server.fetch("https://example.com/a")

    assert "too many redirects" in out
    assert len(seen) == server.REDIRECT_LIMIT + 1


def test_fetch_honours_the_private_host_escape_hatch(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"z" * 400
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(server, "ALLOW_PRIVATE_HOSTS", True)

    assert server.fetch("http://127.0.0.1/x") == "z" * 400


def test_fetch_caps_the_body_by_content_length(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "content-length": str(server.MAX_BODY_BYTES + 1),
            },
            content=b"x",
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    out = server.fetch("https://example.com/big")

    assert "larger than 20 MB cap" in out


def test_fetch_decodes_with_the_declared_charset(monkeypatch):
    body = "Привет, мир! " * 40

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=cp1251"},
            content=body.encode("cp1251"),
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    assert server.fetch("https://example.com/ru").startswith("Привет, мир!")


def test_fetch_defaults_to_utf8_without_a_charset(monkeypatch):
    body = "Привет, мир! " * 40

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=body.encode("utf-8")
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    assert server.fetch("https://example.com/ru").startswith("Привет, мир!")


def test_fetch_rejects_non_http_schemes():
    assert "only http(s)" in server.fetch("ftp://example.com/x")


def test_fetch_caches_and_clamps_maxlen(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"q" * 2000
        )

    monkeypatch.setattr(server, "_new_client", lambda headers: _mock_client(handler, headers))
    monkeypatch.setattr(socket, "getaddrinfo", _public_resolver())

    first = server.fetch("https://example.com/p", maxlen=10)
    second = server.fetch("https://example.com/p", maxlen=10)

    assert len(seen) == 1, "second call must come from the cache"
    assert "[local cache" in second
    assert first.endswith("...[truncated]")
    assert len(first) < 600, "maxlen is clamped up to the floor"


# --- engine routing --------------------------------------------------------


def test_search_caches_and_tags_the_engine(monkeypatch):
    calls = []

    def fake_search_all(query, n, news, region):
        calls.append(query)
        return "ddgs:wt-wt", [{"title": "T", "url": "https://t", "body": "S"}]

    monkeypatch.setattr(server, "_search_all", fake_search_all)

    first = server.search("Same Query")
    second = server.search("same query")

    assert len(calls) == 1
    assert "[engine: ddgs:wt-wt]" in first
    assert "[local cache" in second


def test_search_clamps_max_results(monkeypatch):
    seen = {}

    def fake_search_all(query, n, news, region):
        seen["n"] = n
        return "ddgs:test", [{"title": "T", "url": "u", "body": "b"}]

    monkeypatch.setattr(server, "_search_all", fake_search_all)
    server.search("q", max_results=10**6)

    assert seen["n"] == server.MAX_RESULTS


def test_search_reports_failure_without_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("all engines failed")

    monkeypatch.setattr(server, "_search_all", boom)
    assert server.search("q").startswith("search failed: RuntimeError")


def test_search_news_passes_region_and_caches_per_region(monkeypatch):
    calls = []

    def fake_news(query, n, region=""):
        calls.append(region)
        return "gnews-rss", [{"title": "T", "url": "u", "body": "b"}]

    monkeypatch.setattr(server, "_news_search_all", fake_news)

    server.search_news("q", region="ru-ru")
    server.search_news("q", region="us-en")
    server.search_news("q", region="ru-ru")

    assert calls == ["ru-ru", "us-en"], "regions must not share a cache entry"


@pytest.mark.parametrize(
    "query,region,expected",
    [
        ("news", "", ["wt-wt", "us-en"]),
        ("новости", "", ["ru-ru", "wt-wt"]),
        ("news", "de-de", ["de-de", "wt-wt"]),
        ("news", "wt-wt", ["wt-wt"]),
    ],
)
def test_regions_for(query, region, expected):
    assert server._regions_for(query, region) == expected


@pytest.mark.parametrize(
    "query,region,expected",
    [
        ("news", "", ("en", "US", "US:en")),
        ("новости", "", ("ru", "RU", "RU:ru")),
        ("news", "de-de", ("de", "DE", "DE:de")),
        ("news", "wt-wt", ("en", "US", "US:en")),
        ("news", "bogus", ("en", "US", "US:en")),
    ],
)
def test_gnews_locale(query, region, expected):
    assert server._gnews_locale(query, region) == expected


def test_search_all_tries_region_agnostic_engines_once(monkeypatch):
    counts = {"searxng": 0, "brave": 0, "ddgs": 0, "sleep": 0}

    def bump(name, result):
        def call(*args, **kwargs):
            counts[name] += 1

        return call

    monkeypatch.setattr(server, "_searxng", bump("searxng", None))
    monkeypatch.setattr(server, "_brave", bump("brave", None))
    monkeypatch.setattr(server, "_ddgs", bump("ddgs", None))
    monkeypatch.setattr(
        server.time, "sleep", lambda s: counts.__setitem__("sleep", counts["sleep"] + 1)
    )

    with pytest.raises(RuntimeError, match="all engines failed"):
        server._search_all("q", 5, news=False, region="")

    assert counts == {"searxng": 1, "brave": 1, "ddgs": 2, "sleep": 1}


def test_search_all_returns_the_surviving_region(monkeypatch):
    monkeypatch.setattr(server, "_searxng", lambda q, n, news: None)
    monkeypatch.setattr(server, "_brave", lambda q, n, news: None)
    monkeypatch.setattr(
        server, "_ddgs", lambda q, n, news, region: [{"title": "T"}] if region == "us-en" else None
    )
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    name, results = server._search_all("hello", 3, news=False, region="")

    assert name == "ddgs:us-en"
    assert results


def test_run_chain_falls_through_and_names_the_last_error():
    def boom():
        raise RuntimeError("403 rate limited")

    name, results = server._run_chain(
        [("bad", boom), ("good", lambda: [{"title": "T"}])]
    )
    assert name == "good" and results

    with pytest.raises(RuntimeError) as excinfo:
        server._run_chain([("bad", boom)])
    assert "403 rate limited" in str(excinfo.value)


def test_fmt_numbers_results_and_handles_empty():
    out = server._fmt(
        [{"title": "A", "url": "https://a", "body": "b", "date": "2026-01-01"}]
    )
    assert out.startswith("1. A")
    assert "https://a" in out
    assert server._fmt([]) == "no results"


# --- Google News RSS -------------------------------------------------------


RSS = """<?xml version="1.0"?>
<rss><channel>
<item><title>Hello - Reuters</title><link>https://n/1</link>
<pubDate>Mon, 20 Oct 2026 10:00:00 GMT</pubDate><source>Reuters</source></item>
<item><title>Second</title><link>https://n/2</link>
<pubDate>Tue, 21 Oct 2026 08:30:00 GMT</pubDate></item>
</channel></rss>"""


def test_gnews_rss_parses_items_and_strips_source_suffix(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = RSS

    def fake_get(url, **kwargs):
        captured.update(kwargs.get("params", {}))
        return FakeResponse()

    monkeypatch.setattr(httpx, "get", fake_get)

    items = server._gnews_rss("news", 5, "de-de")

    assert [i["title"] for i in items] == ["Hello", "Second"]
    assert [i["url"] for i in items] == ["https://n/1", "https://n/2"]
    assert items[0]["body"] == "Reuters"
    assert items[0]["date"] == "Mon, 20 Oct 2026"
    assert captured["hl"] == "de" and captured["ceid"] == "DE:de"


def test_gnews_rss_returns_none_on_bad_status(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = ""

    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: FakeResponse())
    assert server._gnews_rss("news", 5) is None


# --- API key discovery -----------------------------------------------------


def test_key_from_env_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WSMCP_TEST_KEY", "env-token")
    (tmp_path / "k.key").write_text("file-token")
    monkeypatch.setattr(server, "_KEYS_DIR", str(tmp_path))
    assert server._key_from("WSMCP_TEST_KEY", "k.key") == "env-token"


def test_key_from_file_is_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("WSMCP_TEST_KEY", raising=False)
    (tmp_path / "k.key").write_text("  file-token\n")
    monkeypatch.setattr(server, "_KEYS_DIR", str(tmp_path))
    assert server._key_from("WSMCP_TEST_KEY", "k.key") == "file-token"


def test_key_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("WSMCP_TEST_KEY", raising=False)
    monkeypatch.setattr(server, "_KEYS_DIR", str(tmp_path))
    assert server._key_from("WSMCP_TEST_KEY", "k.key") is None
