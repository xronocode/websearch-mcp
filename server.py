#!/usr/bin/env python3
"""Local-first, zero-key, zero-quota web search MCP server.

Works with any MCP client (ZCode, Claude Code, Codex, generic MCP) as a stdio
server. Motivation: built-in web search/fetch tools of AI coding plans are
hosted and account-quota-metered - when the quota dies, the agent goes blind
for days. This server runs entirely on your machine: search goes through the
`ddgs` aggregator library (DuckDuckGo/Bing/Brave/Mojeek/Yahoo backends, no API
keys, no signup, no quota). If a Brave Search API key is available (env
BRAVE_API_KEY, or a chmod-600 file at ~/.config/websearch-mcp/brave.key,
overridable via BRAVE_KEY_FILE), the official Brave API is tried first — best
relevance, and its free tier is monthly-capped, so the local cache matters.
If env SEARXNG_URL is set (a local SearXNG with JSON format enabled, optional),
it is tried before Brave.

Tools (each system routed to the engines that fit it; the [engine: ...] tag
in the output shows who answered):
  search(query, ...)          -- web search:    searxng -> Brave -> ddgs(regions)
  search_news(query, ...)     -- news search:   Google News RSS -> Brave -> ddgs
  search_semantic(query, ...) -- neural search: Exa -> Tavily -> ddgs
  search_github(query, ...)   -- GitHub repositories/issues via REST API
  fetch(url, maxlen)          -- GET page/PDF -> readable text

fetch extracts the main content (links + tables kept) via trafilatura, falls
back to raw HTML->text when extraction fails; text-based PDFs via pypdf. Both
search and fetch are cached in cache.sqlite3 next to this file (search 24h,
fetch 7d) so repeats cost zero tokens and zero engine hits.

Security posture (see README for the trust model):
  - fetch allows only http/https and refuses hosts that resolve to
    private/loopback/link-local addresses (SSRF guard); opt back in with
    WEBSEARCH_ALLOW_PRIVATE_HOSTS=1
  - response bodies are capped (20 MB) to avoid memory spikes
  - no shell, no eval, no subprocess; GITHUB_TOKEN (optional) is only ever
    sent as an Authorization header to api.github.com
  - returned page text is untrusted input: a caller (LLM) must treat it as
    data, not instructions (prompt-injection caveat, same as any web tool)

Privacy: queries leave this machine directly to the search engines from your
IP (that is inherent to any search); no third-party hosted service in between.
"""

import hashlib
import logging
import os
import re
import sqlite3
import time

import httpx
from mcp.server.fastmcp import FastMCP

# ddgs logs every engine attempt (403/429 fallbacks) and mcp logs each request
# at INFO -> noise in the stdio error stream ZCode captures; keep WARNING+ only.
logging.disable(logging.INFO)

mcp = FastMCP("websearch")

_CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)

# --- local cache: repeats cost zero tokens and zero engine hits -------------
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.sqlite3")
TTL_SEARCH = 24 * 3600
TTL_FETCH = 7 * 24 * 3600


def _ckey(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def _cache_get(key, ttl):
    try:
        with sqlite3.connect(_DB, timeout=5) as con:
            row = con.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[1] < ttl:
            age_h = int((time.time() - row[1]) // 3600)
            return row[0] + f"\n[local cache, {age_h}h old]"
    except sqlite3.OperationalError:
        pass
    return None


def _cache_put(key, value):
    try:
        with sqlite3.connect(_DB, timeout=5) as con:
            con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
            con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, value, time.time()))
    except sqlite3.OperationalError:
        pass

SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
# API keys live in a chmod-700 dir (default ~/.config/websearch-mcp/<name>.key,
# chmod 600) or in env vars. Each engine joins the chain only when its key is
# present; on any error/empty answer the chain falls through to the next engine.
_KEYS_DIR = os.path.expanduser(
    os.environ.get("WEBSEARCH_KEYS_DIR", "~/.config/websearch-mcp")
)


def _key_from(env_var, filename, extra_path_env=None):
    tok = os.environ.get(env_var, "").strip()
    if tok:
        return tok
    paths = []
    if extra_path_env and os.environ.get(extra_path_env):
        paths.append(os.path.expanduser(os.environ[extra_path_env]))
    paths.append(os.path.join(_KEYS_DIR, filename))
    for p in paths:
        try:
            with open(p) as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            continue
    return None


def _brave(query, n, news):
    key = _key_from("BRAVE_API_KEY", "brave.key", "BRAVE_KEY_FILE")
    if not key:
        return None
    try:
        api = (
            "https://api.search.brave.com/res/v1/news/search"
            if news
            else "https://api.search.brave.com/res/v1/web/search"
        )
        r = httpx.get(
            api,
            params={"q": query, "count": min(n, 20)},
            headers={
                "X-Subscription-Token": key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": UA,
            },
            timeout=12,
        )
        if r.status_code != 200:
            return None  # quota / rate limit / error -> fall through to ddgs
        data = r.json()
        items = data.get("results", []) if news else (data.get("web") or {}).get("results", [])
        out = [
            {
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "body": it.get("description", "") or "",
                "date": it.get("age", ""),
            }
            for it in items[:n]
        ]
        return out or None
    except Exception:
        return None


def _exa(query, n):
    """Exa neural search (semantic 'find articles about X')."""
    key = _key_from("EXA_API_KEY", "exa.key")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"query": query, "numResults": min(n, 10), "type": "auto"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return [
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "body": (x.get("text") or "")[:200],
                "date": (x.get("publishedDate") or "")[:10],
            }
            for x in r.json().get("results", [])
        ] or None
    except Exception:
        return None


def _tavily(query, n):
    """Tavily agent search."""
    key = _key_from("TAVILY_API_KEY", "tavily.key")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query, "max_results": min(n, 10)},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        return [
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "body": (x.get("content") or "")[:200],
                "date": (x.get("published_date") or "")[:10],
            }
            for x in r.json().get("results", [])
        ] or None
    except Exception:
        return None


def _gnews_rss(query, n):
    """Google News RSS: zero-key news search, no scraping."""
    import xml.etree.ElementTree as ET

    if _CYRILLIC.search(query):
        hl, gl, ceid = "ru", "RU", "RU:ru"
    else:
        hl, gl, ceid = "en", "US", "US:en"
    try:
        r = httpx.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
            headers={"User-Agent": UA},
            timeout=12,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.text)
        out = []
        for item in root.findall(".//item")[:n]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            src = (src_el.text or "").strip() if src_el is not None else ""
            if src and title.endswith(" - " + src):
                title = title[: -(len(src) + 3)]
            out.append({"title": title, "url": link, "body": src, "date": pub[:16]})
        return out or None
    except Exception:
        return None


def _run_chain(engines):
    """engines: list of (name, callable -> list|None). Returns (name, results)."""
    last_err = "unknown"
    for name, fn in engines:
        try:
            res = fn()
            if res:
                return name, res
            last_err = f"{name}: no results"
        except Exception as e:
            last_err = f"{name}: {e}"
    raise RuntimeError(f"all engines failed ({last_err})")
ALLOW_PRIVATE_HOSTS = os.environ.get("WEBSEARCH_ALLOW_PRIVATE_HOSTS", "") == "1"
MAX_BODY_BYTES = 20 * 1024 * 1024


def _host_is_public(host: str) -> bool:
    # SSRF guard for the fetch tool: refuse hosts resolving to
    # private/loopback/link-local ranges. TOCTOU vs CDNs re-resolving is
    # accepted for this threat model; final redirect host is re-checked.
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
    return True


def _fmt(results):
    lines = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or r.get("href") or "").strip()
        snip = (r.get("body") or r.get("content") or r.get("snippet") or "").strip()
        date = (r.get("date") or "").strip()
        prefix = f"{i}. {title}"
        if date:
            prefix += f"  [{date}]"
        lines.append(f"{prefix}\n   {url}\n   {snip}")
    return "\n".join(lines) if lines else "no results"


def _searxng(query, n, news):
    if not SEARXNG_URL:
        return None
    try:
        params = {"q": query, "format": "json"}
        if news:
            params["categories"] = "news"
        r = httpx.get(
            f"{SEARXNG_URL}/search",
            params=params,
            timeout=12,
            headers={"User-Agent": UA},
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.json().get("results", [])[:n]
    except Exception:
        return None


def _ddgs(query, n, news, region):
    from ddgs import DDGS

    try:
        d = DDGS(timeout=15)
    except TypeError:  # older ddgs without timeout kwarg
        d = DDGS()
    with d:
        if news:
            return d.news(query, region=region, max_results=n)
        return d.text(query, region=region, safesearch="moderate", max_results=n)


def _regions_for(query, region):
    if region:
        return [region]
    # auto: Cyrillic queries parse far better on ru-ru; engines are flaky
    # per-query, so always keep a second region as fallback.
    return ["ru-ru", "wt-wt"] if _CYRILLIC.search(query) else ["wt-wt", "us-en"]


def _search_all(query, n, news, region):
    """General web search: searxng -> brave -> ddgs (region chain)."""
    last_err = "unknown"
    for reg in _regions_for(query, region):
        for name, fn in (
            ("searxng", lambda: _searxng(query, n, news)),
            ("brave", lambda: _brave(query, n, news)),
            (f"ddgs:{reg}", lambda: _ddgs(query, n, news, reg)),
        ):
            try:
                res = fn()
                if res:
                    return name, res
                last_err = f"{name}: no results"
            except Exception as e:
                last_err = f"{name}: {e}"
        time.sleep(1.2)
    raise RuntimeError(f"all engines failed ({last_err})")


def _news_search_all(query, n):
    """News search: zero-key Google News RSS first (saves API quota),
    then Brave news, then ddgs."""
    return _run_chain(
        [
            ("gnews-rss", lambda: _gnews_rss(query, n)),
            ("brave", lambda: _brave(query, n, news=True)),
            ("ddgs:ru-ru", lambda: _ddgs(query, n, news=True, region="ru-ru")),
            ("ddgs:wt-wt", lambda: _ddgs(query, n, news=True, region="wt-wt")),
        ]
    )


def _html_to_text(html: str, url: str) -> str:
    # main-content extraction first: ~6x fewer tokens than raw html2text
    # (measured 17.8k -> 2.7k chars on a typical bank product page);
    # raw fallback for pages where the extractor finds nothing
    try:
        import trafilatura

        ext = trafilatura.extract(html, url=url, include_links=True, include_tables=True) or ""
        if len(ext) >= 400:
            return ext
    except Exception:
        pass
    import html2text

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0
    return h.handle(html)


@mcp.tool()
def search(query: str, max_results: int = 8, region: str = "") -> str:
    """Free LOCAL web search, no provider quota. DEFAULT choice for web search in this
    environment: try this tool first; use the built-in WebSearch only if this one errors.
    Returns numbered results (title, URL, snippet) and an [engine: ...] tag showing who
    answered. region optional ('ru-ru', 'us-en'); empty = auto (Cyrillic -> ru-ru first).
    Results cached locally for 24h. Docs of libraries need no special engine: search
    'site:docs...' + fetch the page."""
    key = _ckey("search", query.strip().lower(), max_results, region)
    hit = _cache_get(key, TTL_SEARCH)
    if hit is not None:
        return hit
    try:
        name, res = _search_all(query, max_results, news=False, region=region)
    except Exception as e:
        return f"search failed: {type(e).__name__}: {e}"
    out = _fmt(res) + f"\n[engine: {name}]"
    _cache_put(key, out)
    return out


@mcp.tool()
def search_news(query: str, max_results: int = 8, region: str = "") -> str:
    """Free LOCAL news search, no provider quota. Use for fresh announcements, launches,
    tariff/partnership news. Route: Google News RSS (zero-key) -> Brave news -> ddgs.
    Returns title, URL, source, date and an [engine: ...] tag. Results cached 24h."""
    key = _ckey("news", query.strip().lower(), max_results)
    hit = _cache_get(key, TTL_SEARCH)
    if hit is not None:
        return hit
    try:
        name, res = _news_search_all(query, max_results)
    except Exception as e:
        return f"search_news failed: {type(e).__name__}: {e}"
    out = _fmt(res) + f"\n[engine: {name}]"
    _cache_put(key, out)
    return out


@mcp.tool()
def search_semantic(query: str, max_results: int = 6) -> str:
    """Free LOCAL semantic/neural web search. Use for meaning-style research queries —
    'articles about X', 'how companies solve Y', solution discovery — where keyword
    search misses. Route: Exa (neural) -> Tavily -> ddgs fallback; Exa/Tavily join only
    when their keys are installed (~/.config/websearch-mcp/exa.key / tavily.key).
    Returns title, URL, snippet, date and an [engine: ...] tag. Cached 24h."""
    key = _ckey("semantic", query.strip().lower(), max_results)
    hit = _cache_get(key, TTL_SEARCH)
    if hit is not None:
        return hit
    try:
        name, res = _run_chain(
            [
                ("exa", lambda: _exa(query, max_results)),
                ("tavily", lambda: _tavily(query, max_results)),
                ("ddgs:wt-wt", lambda: _ddgs(query, max_results, news=False, region="wt-wt")),
            ]
        )
    except Exception as e:
        return f"search_semantic failed: {type(e).__name__}: {e}"
    out = _fmt(res) + f"\n[engine: {name}]"
    _cache_put(key, out)
    return out


_GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")


@mcp.tool()
def search_github(query: str, kind: str = "repos", max_results: int = 8) -> str:
    """Free LOCAL GitHub search via the official REST API, no key needed (set GITHUB_TOKEN
    env for higher limits). kind='repos': find repositories by topic/name/description
    (sorted by stars). kind='issues': bug reports and discussions. Useful to find tools,
    code examples, whether something is maintained. Cached 24h."""
    key = _ckey("github", kind, query.strip().lower(), max_results)
    hit = _cache_get(key, TTL_SEARCH)
    if hit is not None:
        return hit
    try:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
        if _GH_TOKEN:
            headers["Authorization"] = f"Bearer {_GH_TOKEN}"
        if kind == "issues":
            api = "https://api.github.com/search/issues"
            params = {"q": query, "per_page": max_results}
        else:
            api = "https://api.github.com/search/repositories"
            params = {"q": query, "per_page": max_results, "sort": "stars"}
        r = httpx.get(api, params=params, headers=headers, timeout=15)
        if r.status_code == 403 or r.status_code == 429:
            return ("GitHub rate limit hit (unauthenticated search is ~10/min); "
                    "retry in a minute or set GITHUB_TOKEN env")
        r.raise_for_status()
        items = r.json().get("items", [])
        lines = []
        for i, it in enumerate(items, 1):
            if kind == "issues":
                repo = it.get("repository_url", "").rsplit("/", 1)[-1]
                lines.append(
                    f"{i}. {it.get('title','')} [{it.get('state','')}] {it.get('updated_at','')[:10]}\n"
                    f"   {it.get('html_url','')}\n"
                    f"   repo: {repo} | {(it.get('body') or '')[:160].strip()}"
                )
            else:
                lines.append(
                    f"{i}. {it.get('full_name','')} * {it.get('stargazers_count',0)} "
                    f"[{it.get('language') or '-'}] pushed {it.get('pushed_at','')[:10]}\n"
                    f"   {it.get('html_url','')}\n"
                    f"   {(it.get('description') or '').strip()}"
                )
        out = "\n".join(lines) if lines else "no results"
        if not out.startswith("no results"):
            _cache_put(key, out)
        return out
    except Exception as e:
        return f"search_github failed: {type(e).__name__}: {e}"


@mcp.tool()
def fetch(url: str, maxlen: int = 12000) -> str:
    """Free LOCAL URL fetch, no provider quota: GET the page and return readable text.
    HTML pages -> extracted main content with links and tables kept (much fewer tokens
    than raw dump); PDF -> extracted text (text-based PDFs only, scanned need OCR).
    Only public http/https URLs; response capped at 20 MB. Cached locally for 7 days.
    If a page yields almost no text (JS-rendered or blocked), returns a note to use
    browser automation for that URL."""
    key = _ckey("fetch", url, maxlen)
    hit = _cache_get(key, TTL_FETCH)
    if hit is not None:
        return hit
    try:
        from urllib.parse import urlparse

        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return "fetch failed: only http(s) URLs are supported"
        if not ALLOW_PRIVATE_HOSTS and not _host_is_public(u.hostname):
            return (
                "fetch failed: host resolves to a private/loopback/link-local "
                "address; set WEBSEARCH_ALLOW_PRIVATE_HOSTS=1 to allow local targets"
            )
        headers = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
        with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
            with client.stream("GET", url) as r:
                # re-check the host after redirects: a public URL can bounce
                # somewhere internal
                final_host = urlparse(str(r.url)).hostname or ""
                if not ALLOW_PRIVATE_HOSTS and not _host_is_public(final_host):
                    return "fetch failed: redirect landed on a private address"
                cl = r.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
                    return f"fetch failed: response larger than {MAX_BODY_BYTES // (1024*1024)} MB cap"
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        return "fetch failed: response exceeded 20 MB cap"
                    chunks.append(chunk)
                content = b"".join(chunks)
                ctype = r.headers.get("content-type", "")
                charset = r.charset or "utf-8"
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages[:40])
            if not text.strip():
                return "PDF has no extractable text (likely scanned images; needs OCR)"
        elif "html" in ctype:
            text = _html_to_text(content.decode(charset, errors="replace"), url)
        else:
            text = content.decode(charset, errors="replace")
        out = text[:maxlen] + ("\n...[truncated]" if len(text) > maxlen else "")
        if len(out.strip()) < 150:
            # don't cache the near-empty case: the wall may be gone tomorrow
            return out.strip() + (
                "\n[note: page returned almost no text — likely JS-rendered or "
                "bot-blocked; use browser automation for this URL]"
            )
        _cache_put(key, out)
        return out
    except Exception as e:
        return f"fetch failed: {type(e).__name__}: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
