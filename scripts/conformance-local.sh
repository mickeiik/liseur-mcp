#!/usr/bin/env bash
# Local repro of the CI conformance gate (see .github/workflows/conformance.yml).
# Usage: ./scripts/conformance-local.sh [--suite active | --scenario <name> ...]
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER_PORT="${SERVER_PORT:-8000}"
PROXY_PORT="${PROXY_PORT:-8010}"
TOKEN="${CONFORMANCE_AUTH_TOKEN:-conformance-local-token}"
CONFORMANCE_VERSION="${CONFORMANCE_VERSION:-0.1.16}"

uv sync --frozen

LISEUR_URL="${LISEUR_URL:-http://127.0.0.1:9}" LISEUR_TOKEN="${LISEUR_TOKEN:-local-dummy}" \
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT="$SERVER_PORT" \
MCP_AUTH_TOKEN="$TOKEN" \
MCP_ALLOWED_ORIGINS="http://127.0.0.1:$PROXY_PORT" \
.venv/bin/liseur-mcp > /tmp/liseur-conformance-server.log 2>&1 &
SERVER_PID=$!
python3 scripts/conformance-proxy.py --upstream "http://127.0.0.1:$SERVER_PORT" \
  --listen-port "$PROXY_PORT" --token "$TOKEN" > /tmp/liseur-conformance-proxy.log 2>&1 &
PROXY_PID=$!
trap 'kill $SERVER_PID $PROXY_PID 2>/dev/null' EXIT

for i in $(seq 1 60); do
  if curl -s -o /dev/null -X POST "http://127.0.0.1:$PROXY_PORT/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":0,"method":"ping"}'; then
    echo "proxy ready"
    break
  fi
  sleep 1
  if [ "$i" = 60 ]; then
    echo '--- server log ---'; cat /tmp/liseur-conformance-server.log
    echo '--- proxy log ---'; cat /tmp/liseur-conformance-proxy.log
    exit 1
  fi
done

if [ "$#" -gt 0 ]; then
  # Pass-through, e.g.: ./scripts/conformance-local.sh --scenario tools-list
  npx -y "@modelcontextprotocol/conformance@${CONFORMANCE_VERSION}" server \
    --url "http://127.0.0.1:${PROXY_PORT}/mcp" "$@"
else
  npx -y "@modelcontextprotocol/conformance@${CONFORMANCE_VERSION}" server \
    --url "http://127.0.0.1:${PROXY_PORT}/mcp" \
    --suite active --expected-failures ./conformance-baseline.yml
fi
