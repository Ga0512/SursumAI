from __future__ import annotations

import argparse
import functools
import http.server
import os
import urllib.error
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
CENTRAL_URL = os.environ.get("SURSUMAI_CENTRAL", "http://localhost:8001")


def _forward(headers: http.server.BaseHTTPRequestHandler, central_url: str, method: str, path: str):
    """Forward a request to the central backend and stream the response back.

    Keeps the browser on a single origin (the web server) so the UI works even
    when the whole app is served through a proxy on a different host/machine.

    Returns True if the request was proxied (matched /api), False otherwise.
    """
    target = central_url.rstrip("/") + path[4:]  # strip leading "/api"
    body = None
    length = headers.headers.get("Content-Length")
    if length:
        body = headers.rfile.read(int(length))
    req = urllib.request.Request(target, data=body, method=method)
    for h in ("Content-Type", "Authorization", "Accept"):
        if headers.headers.get(h):
            req.add_header(h, headers.headers[h])
    try:
        with urllib.request.urlopen(req, timeout=300) as upstream:
            status = upstream.status
            ctype = upstream.headers.get("Content-Type", "application/json")
            headers.send_response(status)
            headers.send_header("Content-Type", ctype)
            headers.send_header("Cache-Control", "no-cache")
            headers.end_headers()
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                try:
                    headers.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
            headers.wfile.flush()
    except urllib.error.HTTPError as e:
        payload = e.read()
        headers.send_response(e.code)
        headers.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
        headers.end_headers()
        headers.wfile.write(payload)
    except urllib.error.URLError as e:
        message = f"backend unreachable: {e.reason}".encode()
        headers.send_response(502)
        headers.send_header("Content-Type", "application/json")
        headers.end_headers()
        headers.wfile.write(message)


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def _maybe_proxy(self, method: str):
        if self.path.startswith("/api"):
            _forward(self, CENTRAL_URL, method, self.path)
        else:
            super().do_GET()

    def do_GET(self):
        self._maybe_proxy("GET")

    def do_POST(self):
        if self.path.startswith("/api"):
            _forward(self, CENTRAL_URL, "POST", self.path)
        else:
            self.send_error(404, "only /api/* is accepted on POST")

    def do_DELETE(self):
        if self.path.startswith("/api"):
            _forward(self, CENTRAL_URL, "DELETE", self.path)
        else:
            self.send_error(404, "only /api/* is accepted on DELETE")


def main() -> None:
    parser = argparse.ArgumentParser(description="SursumAI web static server + /api proxy")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    os.chdir(WEB_DIR)
    handler = functools.partial(ProxyHandler, directory=str(WEB_DIR))
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"SursumAI web on http://{args.host}:{args.port} (api → {CENTRAL_URL})")
        httpd.serve_forever()


if __name__ == "__main__":
    main()