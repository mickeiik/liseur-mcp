# liseur-mcp

Read-only MCP server for a [liseur-sync](https://github.com/chmouel/liseur-sync)
instance: book catalog, reading statistics, highlights and EPUB chapter text,
for any MCP client (opencode, Claude Desktop/Code, Cursor, ...).

It talks to the native `/v1` API with a device token you mint for it. It never
writes to the catalog or your reading state; the only side effect is that
`list_highlights(book_id=...)` resolves the book to your per-user reading work,
the same mapping every reading client makes.

## Tools

| Tool | What it does |
| --- | --- |
| `list_folders` | folders this account can read |
| `list_books` | books in a folder, newest first |
| `search_books` | search titles, descriptions, series, contributors, tags |
| `get_book` | one catalog record by id |
| `reading_stats` | totals, streak, pace, plus per-work rows |
| `list_highlights` | highlights/notes/bookmarks, for one book or the account |
| `get_book_text` | table of contents and chapter text (EPUB parsed locally) |

## Scopes

Mint a dedicated device token with exactly:

- `library-read` — folders, books, search, download
- `read-insights` — reading statistics
- `sync` — highlights and notes; the book→work join needs this and `library-read`

## Run (stdio, for clients on this machine)

```sh
uv sync
export LISEUR_URL=https://books.example.com
export LISEUR_TOKEN=...
uv run liseur-mcp
```

opencode example (`opencode.json`), pointing at the venv binary so no uv lookup happens
at startup:

```jsonc
{
  "mcp": {
    "servers": {
      "liseur": {
        "type": "local",
        "command": ["/path/to/liseur-mcp/.venv/bin/liseur-mcp"],
        "environment": {
          "LISEUR_URL": "https://books.example.com",
          "LISEUR_TOKEN": "{env:LISEUR_TOKEN}"
        }
      }
    }
  }
}
```

## Run (streamable HTTP, one endpoint for several agents)

```sh
export MCP_TRANSPORT=streamable-http
export MCP_HOST=0.0.0.0
export MCP_AUTH_TOKEN=...   # required: the endpoint has no anonymous mode
# every Host header a client reaches this server under; `name:*` accepts any port
export MCP_ALLOWED_HOSTS=books.example.com,books.example.com:*,localhost:*,127.0.0.1:*
export LISEUR_URL=... LISEUR_TOKEN=...
uv run liseur-mcp
```

Clients connect to `http://<host>:8000/mcp` with
`Authorization: Bearer $MCP_AUTH_TOKEN`.

```jsonc
{
  "mcp": {
    "servers": {
      "liseur": {
        "type": "remote",
        "url": "http://<host>:8000/mcp",
        "oauth": false,
        "headers": { "Authorization": "Bearer {env:LISEUR_MCP_TOKEN}" }
      }
    }
  }
}
```

Keep it on your LAN or behind your reverse proxy; the bearer token is the only door.

## Docker

```sh
docker build -t liseur-mcp .
docker run -d --name liseur-mcp --restart unless-stopped \
  -e MCP_TRANSPORT=streamable-http -e MCP_HOST=0.0.0.0 \
  -e MCP_AUTH_TOKEN -e LISEUR_URL -e LISEUR_TOKEN \
  -e MCP_ALLOWED_HOSTS=books.example.com,books.example.com:*,localhost:*,127.0.0.1:* \
  -p 8000:8000 liseur-mcp
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `LISEUR_URL` | required | base URL of the instance |
| `LISEUR_TOKEN` / `LISEUR_TOKEN_FILE` | required | device token secret |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `127.0.0.1` / `8000` / `/mcp` | HTTP listener |
| `MCP_AUTH_TOKEN` / `MCP_AUTH_TOKEN_FILE` | required for HTTP | bearer token clients present |
| `MCP_ALLOWED_HOSTS` | localhost, 127.0.0.1 (any port) | Host headers the HTTP transport accepts |
| `MCP_ALLOWED_ORIGINS` | none | Origin headers accepted, listed exactly (no `*` wildcard); with none, any request carrying an Origin is refused |
| `LOG_LEVEL` | `INFO` | log level |
| `LISEUR_TIMEOUT_SECONDS` | `30` | upstream request timeout |

### If a client cannot connect

- `421 Invalid Host header` — the request arrived under a Host the transport
  refuses. Add the name you connect with to `MCP_ALLOWED_HOSTS`; entries match
  exactly, so write `name:*` to accept any port (`localhost` alone does not
  match `Host: localhost:8000`).
- `403 Invalid Origin header` — the client sends an `Origin` and
  `MCP_ALLOWED_ORIGINS` is empty; list that origin.
- `403 {"error":"https required"}` — that comes from the liseur-sync instance,
  not from here: it refuses plain HTTP unless it is configured to allow it.
  Point `LISEUR_URL` at the HTTPS name.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check
uv run pyright
```

## Conformance

The MCP spec conformance gate runs the official
[`modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance)
suite (`server`, `active`) in CI (`.github/workflows/conformance.yml`, pinned
to `v0.1.16`). The harness sends no auth header, so runs put a small
auth-injecting proxy (`scripts/conformance-proxy.py`) in front of the server.
Known-by-design failures (this tools-only server exposes no resources,
prompts, completions, elicitation or sampling) are baselined in
`conformance-baseline.yml`. No liseur-sync instance is needed: the
protocol-level scenarios never invoke the real tools.

```sh
./scripts/conformance-local.sh
# or a single scenario: ./scripts/conformance-local.sh --scenario tools-list
```
