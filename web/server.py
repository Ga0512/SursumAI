from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
CENTRAL_URL = os.environ.get("SURSUMAI_CENTRAL", "http://localhost:8001")


def _forward(headers: http.server.BaseHTTPRequestHandler, central_url: str, method: str, path: str):
    """Forward a request to the central backend and stream the response back.

    Keeps the browser on a single origin (the web server) so the UI works even
    when the whole app is served through a proxy on a different host/machine.

    Streaming (SSE) requires HTTP/1.1 + Transfer-Encoding: chunked — HTTP/1.0
    close-delimited responses make browsers buffer until EOF (the whole reply
    arrives at once instead of token by token).

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
            headers.send_header("Connection", "close")
            headers.send_header("Transfer-Encoding", "chunked")
            headers.end_headers()
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                try:
                    headers.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    headers.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            try:
                headers.wfile.write(b"0\r\n\r\n")
                headers.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
    except urllib.error.HTTPError as e:
        payload = e.read()
        headers.send_response(e.code)
        headers.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
        headers.send_header("Content-Length", str(len(payload)))
        headers.end_headers()
        headers.wfile.write(payload)
    except urllib.error.URLError as e:
        message = f"backend unreachable: {e.reason}".encode()
        headers.send_response(502)
        headers.send_header("Content-Type", "application/json")
        headers.send_header("Content-Length", str(len(message)))
        headers.end_headers()
        headers.wfile.write(message)


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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


def _utf8_output() -> None:
    """Never die printing a status line.

    stdout is redirected to a log file by start.sh, so its encoding comes from
    the locale — under LANG=C (or a Windows console) an arrow or a check mark
    raises UnicodeEncodeError and takes the whole process down at boot.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable stream
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="SursumAI web static server + /api proxy")
    parser.add_argument("--port", type=int, default=3000)
    # loopback by default: the UI is for this machine. Exposing it on the
    # network is opt-in via SURSUMAI_BIND (or an explicit --host).
    parser.add_argument("--host", default=os.environ.get("SURSUMAI_BIND", "127.0.0.1"))
    args = parser.parse_args()

    _utf8_output()
    os.chdir(WEB_DIR)
    handler = functools.partial(ProxyHandler, directory=str(WEB_DIR))
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"SursumAI web on http://{args.host}:{args.port} (api → {CENTRAL_URL})")
        httpd.serve_forever()


if __name__ == "__main__":
    main()