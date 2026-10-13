from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import ports  # noqa: E402

CENTRAL_URL = os.environ.get("SURSUMAI_CENTRAL", f"http://localhost:{ports.CENTRAL_PORT}")

# A proxy has to outlive what it proxies. The central waits
# SURSUMAI_CHAT_TIMEOUT (900 s) for a model that reasons; this was a hard
# 300 s, and the timeout applies to every read, so on a slow machine the
# proxy gave up first. The TimeoutError is neither HTTPError nor URLError,
# so it escaped uncaught after the headers had already been sent, the
# connection died mid-response, and the page reported "Could not reach
# server" while the model was alive and still answering.
UPSTREAM_TIMEOUT = float(os.environ.get("SURSUMAI_CHAT_TIMEOUT", 900)) + 60


def _stream_error(headers: http.server.BaseHTTPRequestHandler, ctype: str,
                  message: str) -> None:
    """Report a failure that happened after the response already started.

    web/app.js acts on a `data:` frame carrying `error`, so this reaches the
    user as a sentence. A truncated body can only reach them as a guess.
    """
    if "event-stream" not in ctype:
        return                  # a plain JSON body: truncation is all we have
    frame = ("data: " + json.dumps({"error": message}) + "\n\n").encode()
    try:
        headers.wfile.write(f"{len(frame):X}\r\n".encode() + frame + b"\r\n")
        headers.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _forward(headers: http.server.BaseHTTPRequestHandler, central_url: str, method: str, path: str):
    """Forward a request to the central backend and stream the response back.

    Keeps the browser on a single origin (the web server) so the UI works even
    when the whole app is served through a proxy on a different host/machine.

    Streaming (SSE) requires HTTP/1.1 + Transfer-Encoding: chunked — HTTP/1.0
    close-delimited responses make browsers buffer until EOF (the whole reply
    arrives at once instead of token by token). It also requires reading the
    upstream with read1 and not sending Connection: close, both below.

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
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as upstream:
            status = upstream.status
            ctype = upstream.headers.get("Content-Type", "application/json")
            headers.send_response(status)
            headers.send_header("Content-Type", ctype)
            headers.send_header("Cache-Control", "no-cache")
            headers.send_header("Transfer-Encoding", "chunked")
            headers.end_headers()
            while True:
                # read1, never read: HTTPResponse.read(n) blocks until it
                # has n bytes or the body ends, so every token of a
                # streamed reply sat here until the model finished.
                # Measured against a backend emitting one token a second
                # for five seconds: read(65536) returned nothing for 5.0 s
                # and then 255 bytes at once, while read1 handed over each
                # token as it arrived. On a fast GPU the whole answer lands
                # in two seconds and the bug is invisible; on a laptop it
                # is minutes of a blank screen.
                try:
                    chunk = upstream.read1(65536)
                except TimeoutError:
                    # The headers are long gone, so there is no status code
                    # left to send. Say it in the stream the page is already
                    # reading; dropping the socket is what made a slow model
                    # indistinguishable from an unreachable server.
                    _stream_error(headers, ctype, "the model took too long"
                                  " to answer - raise SURSUMAI_CHAT_TIMEOUT")
                    break
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
    parser.add_argument("--port", type=int, default=ports.WEB_PORT)
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