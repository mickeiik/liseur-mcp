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
- `sync` — highlights and notes, and the book→work join

## Run (stdio, for clients on this machine)

```sh
uv sync
export LISEUR_URL=https://books.example.ts.net
export LISEUR_TOKEN=...
uv run liseur-mcp
```

opencode (`opencode.json`), pointing at the venv binary so no uv lookup happens
at startup:

```jsonc
{
  "mcp": {
    "servers": {
      "liseur": {
        "type": "local",
        "command": ["/path/to/liseur-mcp/.venv/bin/liseur-mcp"],
        "environment": {
          "LISEUR_URL": "https://books.example.ts.net",
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
export LISEUR_URL=... LISEUR_TOKEN=...
uv run liseur-mcp
```

Clients connect to `http://<host>:8000/mcp` with
`Authorization: Bearer $MCP_AUTH_TOKEN`. In opencode:

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
| `MCP_ALLOWED_HOSTS` | localhost | Host headers the HTTP transport accepts |
| `LISEUR_TIMEOUT_SECONDS` | `30` | upstream request timeout |

## Develop

```sh
uv sync
uv run pytest
uv run ruff check
uv run pyright
```
