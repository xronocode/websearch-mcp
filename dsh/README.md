# dsh-mcp-websearch

A DeepSeek Harness bundle that connects [`websearch-mcp`](https://github.com/xronocode/websearch-mcp) —
a local-first, zero-key MCP server — so the agent gets five web tools without any
hosted search quota:

| Tool | What it does |
|---|---|
| `mcp__websearch__search` | Web search via SearXNG → Brave (optional key) → `ddgs` engine chain |
| `mcp__websearch__search_news` | News with dates and sources; `region` selects the Google News edition |
| `mcp__websearch__search_semantic` | Neural search via Exa/Tavily when keys exist, `ddgs` otherwise |
| `mcp__websearch__search_github` | GitHub repositories and issues through the REST API, no token required |
| `mcp__websearch__fetch` | Reader-mode page/PDF text, hop-vetted redirects, local cache |

Results are cached in a local SQLite file (search 24 h, pages 7 days), so repeats cost
zero engine hits. Everything runs on this machine; queries go straight from your IP to
the search engines.

## Install

This is a **configuration-only bundle**: it contains no code, only a Loader patch that
mounts `@deepseek-ai/dsh-mcp-client` with this server as a stdio child. Install it in the
current profile:

```
plugin_manager install_bundle target: <path-to-this-directory>
```

or, once published, by package name. The bundle adds one row, `mcp-websearch`, and the
tools appear under the `websearch` namespace.

## Requirements

- `uv` on `PATH` — the default launcher is `uvx`, which installs and caches the Python
  server on first start (`uvx --from git+https://github.com/xronocode/websearch-mcp websearch-mcp`).
  The Harness spawns the child with its own `PATH`: a Dock-launched app may not see
  `~/.local/bin`, so set `WEBSEARCH_MCP_COMMAND` to the absolute `uvx` path if the tools
  do not appear.
- Python ≥ 3.10 (uv provisions it if needed) and network access for the first start.

Already have a checkout and a venv? Point the bundle at it instead of `uvx`:

```bash
WEBSEARCH_MCP_COMMAND=/path/to/venv/bin/python
WEBSEARCH_MCP_ARGS=/path/to/server.py
```

`WEBSEARCH_MCP_ARGS` is split on spaces, so a single script path works out of the box.

## Configuration

| Variable | Effect |
|---|---|
| `WEBSEARCH_MCP_COMMAND` | Interpreter or launcher; default `uvx` |
| `WEBSEARCH_MCP_ARGS` | Space-separated arguments; default `--from git+…/websearch-mcp websearch-mcp` |

The server's own optional keys (`BRAVE_API_KEY`, `EXA_API_KEY`, `TAVILY_API_KEY`,
`GITHUB_TOKEN`, `SEARXNG_URL`, `WEBSEARCH_ALLOW_PRIVATE_HOSTS`) are read from the child
process environment; see the [server README](https://github.com/xronocode/websearch-mcp#readme).

## Security notes

- Fetched pages and search snippets are untrusted web content: the model must treat them
  as data, never as instructions.
- `fetch` refuses private, loopback and link-local hosts, re-checking every redirect hop
  before it is sent; `WEBSEARCH_ALLOW_PRIVATE_HOSTS=1` opts back in.
- No shell, no `eval`, no subprocesses in the server; the only outbound HTTP is to the
  configured search engines, GitHub, and the fetched URLs.

## License

MIT, same as the server. The icon is original artwork shipped with this package.
