"""Bearer-injecting reverse proxy for MCP conformance runs.

The official conformance harness sends no Authorization header, but
liseur-mcp's streamable-http transport requires bearer auth (see
``src/liseur_mcp/auth.py``). This proxy injects the header and forwards
everything else untouched -- in particular the Host header, which the
dns-rebinding-protection scenario varies. Threaded, so the harness's
long-lived SSE streams do not block other requests.

Test-only tool (CI gate and local repro); never used in production.
Stdlib only.
"""

from __future__ import annotations

import argparse
import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_UPSTREAM_HOST = "127.0.0.1"
_UPSTREAM_PORT = 8000
_TOKEN = ""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ConformanceAuthProxy/1.0"

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        upstream = http.client.HTTPConnection(_UPSTREAM_HOST, _UPSTREAM_PORT, timeout=30)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in ("content-length", "authorization")
        }
        # Host is forwarded unchanged: rebinding checks depend on it.
        headers["Authorization"] = f"Bearer {_TOKEN}"
        headers["Connection"] = "close"
        if body is not None:
            headers["Content-Length"] = str(len(body))
        upstream.request(self.command, self.path, body=body, headers=headers)
        response = upstream.getresponse()
        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() in ("transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.end_headers()
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    do_GET = _forward
    do_POST = _forward
    do_DELETE = _forward

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:8000")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8010)
    parser.add_argument("--token", default=os.environ.get("MCP_AUTH_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("provide --token or set MCP_AUTH_TOKEN")
    global _UPSTREAM_HOST, _UPSTREAM_PORT, _TOKEN
    parts = urlsplit(args.upstream)
    _UPSTREAM_HOST = parts.hostname or "127.0.0.1"
    _UPSTREAM_PORT = parts.port or 80
    _TOKEN = args.token
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
