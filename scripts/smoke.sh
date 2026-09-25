#!/usr/bin/env bash
# Smoke tests against the shipped binary using the official MCP Inspector CLI.
# Proves the real entry point starts and speaks MCP over both transports with
# the expected tool surface and portable schemas. Upstream calls are not
# exercised: tools/call probes use invalid arguments, refused before any
# liseur-sync request. See docs/cli-smoke-testing.md in modelcontextprotocol/inspector.
#
# Usage: ./scripts/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

INSPECTOR_VERSION="${INSPECTOR_VERSION:-2.8.0}"
HTTP_PORT="${HTTP_PORT:-8000}"
TOKEN="smoke-local-token"
INSPECTOR=(npx --yes "@modelcontextprotocol/inspector@${INSPECTOR_VERSION}" --cli)
TOOLS='["list_folders","list_books","search_books","get_book","reading_stats","list_highlights","get_book_text"]'

# Keep the Inspector away from a developer's real OAuth store (both variables:
# the explicit path wins over MCP_STORAGE_DIR).
export MCP_STORAGE_DIR="$(mktemp -d)"
export MCP_INSPECTOR_OAUTH_STATE_PATH="$MCP_STORAGE_DIR/oauth.json"
trap 'rm -rf "$MCP_STORAGE_DIR"' EXIT

# Shared assertion: the 7 tools are present. Reads JSON envelope on stdin.
assert_tools() {
  jq -e --argjson want "$TOOLS" \
    '[.result.tools[].name] as $have | ($want - $have | length == 0) and ($have - $want | length == 0)' \
    > /dev/null
}

# Assert the call failed as a tool error (exit 5), not a crash or unreachable server.
expect_tool_error() {
  local status=0
  "$@" > /dev/null 2>&1 || status=$?
  if [ "$status" -ne 5 ]; then
    echo "expected tool error (exit 5), got exit $status: $*" >&2
    return 1
  fi
}

echo "=== stdio transport ==="
STDIO=(.venv/bin/liseur-mcp
  -e LISEUR_URL=http://127.0.0.1:9 -e LISEUR_TOKEN=smoke-dummy
  --connect-timeout 15000)

"${INSPECTOR[@]}" "${STDIO[@]}" --method initialize --format json \
  | jq -e '.result.protocolVersion and .result.serverInfo.name == "liseur"' > /dev/null
echo "ok: initialize"

"${INSPECTOR[@]}" "${STDIO[@]}" --method tools/list --format json | assert_tools
echo "ok: 7 tools present"

"${INSPECTOR[@]}" "${STDIO[@]}" --method tools/list --strict --format json \
  | jq -e '[.schemaFindings[]?.findings[]? | select(.severity == "error")] | length == 0' > /dev/null
echo "ok: schemas portable"

expect_tool_error "${INSPECTOR[@]}" "${STDIO[@]}" --method tools/call \
  --tool-name reading_stats --tool-args-json '{"range":"banana"}'
echo "ok: invalid argument refused"

echo "=== http transport ==="
LISEUR_URL=http://127.0.0.1:9 LISEUR_TOKEN=smoke-dummy \
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT="$HTTP_PORT" \
MCP_AUTH_TOKEN="$TOKEN" \
  .venv/bin/liseur-mcp > /tmp/liseur-smoke-server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; rm -rf "$MCP_STORAGE_DIR"' EXIT

for i in $(seq 1 30); do
  if curl -s -o /dev/null -X POST "http://127.0.0.1:${HTTP_PORT}/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","id":0,"method":"ping"}'; then
    break
  fi
  sleep 1
  if [ "$i" = 30 ]; then
    echo "server did not become ready" >&2
    cat /tmp/liseur-smoke-server.log >&2
    exit 1
  fi
done

HTTP=("${INSPECTOR[@]}" --transport http --server-url "http://127.0.0.1:${HTTP_PORT}/mcp" \
  --header "Authorization: Bearer $TOKEN" --connect-timeout 15000)

"${HTTP[@]}" --method initialize --format json \
  | jq -e '.result.serverInfo.name == "liseur"' > /dev/null
echo "ok: initialize"

"${HTTP[@]}" --method tools/list --format json | assert_tools
echo "ok: 7 tools present"

expect_tool_error "${HTTP[@]}" --method tools/call --tool-name get_book --tool-args-json '{}'
echo "ok: missing required argument refused"

echo "smoke OK"
