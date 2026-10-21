# websearch-mcp

Local-first, **zero-key, zero-quota** web search MCP server. Four lean tools that give any MCP client (ZCode, Claude Code, Codex, generic MCP) web search, news search, GitHub search and a token-efficient page/PDF fetcher — with no API keys, no signups and no hosted service metering your account.

**Why:** built-in web search tools of AI coding plans are hosted and account-quota-metered. When the quota runs out, the agent goes blind for days. This server runs entirely on your machine: search goes through the [`ddgs`](https://pypi.org/project/ddgs/) aggregator library, which queries DuckDuckGo / Bing / Brave / Mojeek / Yahoo and falls back across engines when one rate-limits (403/429 are routine and handled). If you have a Brave Search API key, that official API is tried first instead.

## Tools (4)

| Tool | What it does |
|---|---|
| `search(query, max_results?, region?)` | Web search: numbered title / URL / snippet. Cyrillic queries automatically try `ru-ru` first, then `wt-wt`; Latin — `wt-wt`, then `us-en`. |
| `search_news(query, max_results?, region?)` | News search with dates. |
| `search_github(query, kind?, max_results?)` | GitHub via the official REST API, **no token needed** (`repos` sorted by stars / `issues`). Set `GITHUB_TOKEN` for higher limits. |
| `fetch(url, maxlen?)` | GET a page → reader-mode main content (links + tables kept, via `trafilatura`; ~6× fewer tokens than a raw dump), falling back to raw HTML→text when extraction finds nothing. Text-based PDFs supported. JS-walled pages are detected and return a hint to use browser automation instead of dumping noise. |

Everything is cached in a local sqlite file next to the script (search 24 h, pages 7 d), so repeated queries cost **zero tokens and zero engine hits** — cache hits are marked `[local cache, Nh old]`.

## Install

Requires [uv](https://docs.astral.sh/uv/) (or any Python ≥ 3.10 with pip) — Python 3.12 recommended.

```bash
# from this repo
uv venv ~/.venvs/websearch-mcp --python 3.12
uv pip install --python ~/.venvs/websearch-mcp git+https://github.com/xronocode/websearch-mcp
```

Then register with your MCP client (user scope, adjust paths to your machine). Example for ZCode (`~/.zcode/cli/config.json`) — Claude Code / Codex / others use their equivalent config:

```json
{
  "mcp": {
    "servers": {
      "websearch": {
        "type": "stdio",
        "command": "/home/you/.venvs/websearch-mcp/bin/websearch-mcp",
        "enabled": true
      }
    }
  }
}
```

Or run from a source checkout: `command`: `<python>`, `args`: `["/path/to/server.py"]`. No templates are expanded — use absolute paths.

### Environment variables (all optional)

| Var | Effect |
|---|---|
| `BRAVE_API_KEY` or `~/.config/websearch-mcp/brave.key` | Use the official [Brave Search API](https://brave.com/search/api/) first — best relevance; the free tier is monthly-capped, and the local cache stretches it. `BRAVE_KEY_FILE` overrides the path. |
| `SEARXNG_URL` | Try a SearXNG instance with JSON format enabled before Brave/`ddgs`. |
| `GITHUB_TOKEN` | Raises GitHub API rate limits for `search_github`. Only ever sent as an Authorization header to `api.github.com`. |
| `WEBSEARCH_ALLOW_PRIVATE_HOSTS` | `1` lets `fetch` reach loopback/private hosts (blocked by default). |

## Comparison (as of Oct 2026 — check upstreams, this space moves fast)

| | **websearch-mcp** | [free-search-mcp](https://github.com/sweetcornna/free-search-mcp) | [open-webSearch](https://github.com/Aas-ee/open-webSearch) | [wigolo](https://github.com/KnockOutEZ/wigolo) | [duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server) | hosted built-ins |
|---|---|---|---|---|---|---|
| API keys / quota | none / none | none / none | none / none | none / none | none / none | account quota |
| Footprint | 1 venv, ~200 MB deps | Python, 11 tools | MCP + CLI + daemon | Node ≥ 20, **~1.5 GB** incl. browser engine + on-device models | light | zero local |
| Engines | Brave API (optional key) → ddgs chain + region fallback | multi-engine, RRF-merged | bing/baidu/ddg/brave/startpage/sogou/HN (CN-leaning) | many | DDG only | vendor |
| Fetch + extraction | trafilatura + raw fallback | trafilatura + more | article fetch | yes | fetch + parse | yes (some digest server-side) |
| PDF | text-based | PDF/DOCX/XLSX/PPTX/EPUB | ✗ | yes | ✗ | varies |
| Local cache | sqlite, 24 h / 7 d | FTS5 | ✗ | yes | ✗ | ✗ |
| Cyrillic auto-region | ✅ | ✗ | ✗ | ✗ | ✗ | ✗ |
| GitHub search | ✅ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Browser dependency | none (JS pages → your browser tool) | optional Chromium for some engines | ✗ | ships its own | ✗ | n/a |
| Per-session tool-schema cost | 4 lean tools | 11 rich tools | few | many | few | n/a |
| License | MIT | MIT | MIT | AGPL-3.0 | MIT | — |

**When to pick something else:** free-search-mcp if you want the research-combine-harvester (RRF merge, doc formats, one-shot `research()`) and don't mind the 11-tool context tax and an external dependency; wigolo if you want the full local web intelligence stack and have 1.5 GB + GPU to spare; open-webSearch if your targets are CN dev resources; hosted built-ins when your quota is alive and quality matters most.

## Security & trust model

Audited (manual + local model review) before first publish. Posture:

- `fetch` allows only `http/https` and refuses hosts that resolve to private / loopback / link-local addresses, including the final host after redirects (SSRF guard). Opt in explicitly with `WEBSEARCH_ALLOW_PRIVATE_HOSTS=1`.
- Response bodies are capped at 20 MB; all outbound calls have timeouts; PDF parsing is capped at 40 pages.
- No shell, no `eval`, no subprocesses. No secrets in code; `GITHUB_TOKEN` is read from the environment and only ever sent to `api.github.com`.
- **Prompt injection:** text returned by `fetch`/`search` comes from the open web and must be treated by the caller (LLM or human) as untrusted data, not instructions. This caveat applies to every web tool.
- **Privacy:** queries leave your machine directly to the search engines from your IP (inherent to any search). No third-party service sits in between, and nothing about your queries is sent anywhere else.

## Limitations (honest)

- Search quality is below a live Google-grade engine: some engines rate-limit per IP (the chain survives, individual engines come and go; the cache absorbs repeats). For rare queries a hosted engine still wins.
- JS-rendered or bot-walled pages return little text — that's the browser-automation signal; this server deliberately does not bundle a browser.
- Scanned PDFs (image-only) are not OCR'd.

## License

[MIT](LICENSE)
