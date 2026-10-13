"""The /api proxy in web/server.py, against a real socket that streams slowly.

Nothing had ever tested this file, and it had never actually streamed: the loop
read the upstream with `read(65536)`, which blocks until it has 65536 bytes or
the body ends, so every token of a reply was held until the model finished. On
a GPU the whole answer lands in two seconds and the bug is invisible; on a
laptop it is minutes of a blank page, and then "Could not reach server" when
the proxy's own timeout fired and dropped the connection.

A fake central emits one token per second here. A proxy that buffers passes
every assertion about *content* and fails only on *timing*, so timing is what
these tests measure.
"""

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from web import server as web_server

TOKENS = 4
TICK = 0.3          # a slow model, compressed


def _central(first_byte_delay=0.0, stream=True):
    """A fake central that answers /deploys/x/chat the way the real one does."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            if not stream:
                body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            time.sleep(first_byte_delay)
            # the timeout test makes the proxy hang up on us halfway. A fake
            # backend that dies of that prints a traceback on a passing run,
            # which is how a green suite starts looking broken.
            try:
                for i in range(TOKENS):
                    frame = json.dumps(
                        {"choices": [{"delta": {"content": f"tok{i} "}}]}
                    ).encode()
                    frame = b"data: " + frame + b"\n\n"
                    self.wfile.write(b"%X\r\n" % len(frame) + frame + b"\r\n")
                    self.wfile.flush()
                    time.sleep(TICK)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _proxy(central, chat_timeout=900):
    """The real ProxyHandler, pointed at the fake central."""
    monkey = {
        "CENTRAL_URL": f"http://127.0.0.1:{central.server_address[1]}",
        "UPSTREAM_TIMEOUT": float(chat_timeout) + 60,
    }
    saved = {k: getattr(web_server, k) for k in monkey}
    for k, v in monkey.items():
        setattr(web_server, k, v)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web_server.ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, saved


def _post(srv, path="/api/deploys/x/chat"):
    """Returns (response, [(seconds, bytes), ...]) with arrival times."""
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request("POST", path, body=b'{"stream":true}',
                 headers={"Content-Type": "application/json"})
    res = conn.getresponse()
    parts = []
    while True:
        part = res.read1(65536)
        if not part:
            break
        parts.append((time.time() - t0, part))
    conn.close()
    return res, parts


@pytest.fixture
def plumbing():
    made = []

    def build(first_byte_delay=0.0, chat_timeout=900, stream=True):
        central = _central(first_byte_delay, stream)
        proxy, saved = _proxy(central, chat_timeout)
        made.append((central, proxy, saved))
        return proxy

    yield build
    for central, proxy, saved in made:
        proxy.shutdown()
        proxy.server_close()
        central.shutdown()
        central.server_close()
        for k, v in saved.items():
            setattr(web_server, k, v)


def test_tokens_arrive_as_they_are_produced(plumbing):
    """The regression. With read() every arrival landed at the same instant."""
    res, parts = _post(plumbing())
    assert res.status == 200
    assert len(parts) == TOKENS, "the proxy coalesced the stream"
    spread = parts[-1][0] - parts[0][0]
    assert spread > TICK * (TOKENS - 2), f"arrived in {spread:.2f}s -- buffered"
    body = b"".join(p for _, p in parts)
    assert b"tok0" in body and f"tok{TOKENS - 1}".encode() in body


def test_no_connection_close_header(plumbing):
    """Connection: close contradicts chunked and kills keep-alive for the page."""
    res, _ = _post(plumbing())
    assert res.headers.get("Transfer-Encoding") == "chunked"
    assert (res.headers.get("Connection") or "").lower() != "close"


def test_slow_backend_is_reported_not_dropped(plumbing):
    """A proxy timeout fires after the headers are gone, so there is no status
    code left to send. It has to reach the page as a sentence -- a dead socket
    is what made a slow model look like an unreachable server."""
    # -59.7 leaves the proxy 0.3s: shorter than the backend's first byte.
    proxy = plumbing(first_byte_delay=TICK * 4, chat_timeout=-59.7)
    res, parts = _post(proxy)
    assert res.status == 200
    body = b"".join(p for _, p in parts).decode()
    assert '"error"' in body, f"socket died silently: {body!r}"
    assert "SURSUMAI_CHAT_TIMEOUT" in body


def test_proxy_outlives_the_central(plumbing):
    """The proxy's budget must exceed the central's, or it manufactures a
    failure the central would have reported properly."""
    import os
    from importlib import reload

    os.environ["SURSUMAI_CHAT_TIMEOUT"] = "900"
    try:
        mod = reload(web_server)
        assert mod.UPSTREAM_TIMEOUT > 900
    finally:
        os.environ.pop("SURSUMAI_CHAT_TIMEOUT", None)
        reload(web_server)


def test_plain_json_still_works(plumbing):
    """Not everything through /api streams; the common case must not regress."""
    res, parts = _post(plumbing(stream=False))
    assert res.status == 200
    assert json.loads(b"".join(p for _, p in parts))["choices"][0]["message"]["content"] == "hi"
