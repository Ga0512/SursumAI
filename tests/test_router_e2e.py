"""End-to-end routing against real HTTP model servers.

Two OpenAI-compatible servers run on real sockets and speak real SSE. The
central, the router and the streaming path are the actual code — only the
models are fake. This is what proves the stream reassembles correctly and
names the model that answered.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from central import app as central_app  # noqa: E402
from central.db import DeployState, Store  # noqa: E402

TOKENS = 12


def _make_handler(name, api_key):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _authed(self):
            if self.headers.get("Authorization") == f"Bearer {api_key}":
                return True
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self._authed():
                self._json({"data": [{"id": name}]})

        def do_POST(self):
            if not self._authed():
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = json.dumps(body.get("messages", []))

            if "routing judge" in prompt:  # the judge: never escalate
                return self._json({
                    "choices": [{"message": {"content": '{"escalate": false}'}}],
                    "usage": {"total_tokens": 5},
                })

            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i in range(TOKENS):
                    chunk = {
                        "id": "c1", "object": "chat.completion.chunk", "model": name,
                        "choices": [{"index": 0, "delta": {"content": f"{name}{i} "},
                                     "finish_reason": None}],
                    }
                    self._chunk(f"data: {json.dumps(chunk)}\n\n")
                self._chunk("data: [DONE]\n\n")
                self._chunk("")
                return

            self._json({
                "choices": [{"message": {"role": "assistant", "content": _full_text(name)},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": TOKENS},
            })

        def _chunk(self, text):
            data = text.encode()
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

    return Handler


def _full_text(name):
    return "".join(f"{name}{i} " for i in range(TOKENS))


@pytest.fixture
def model_server():
    """Start a fake model on an ephemeral port; returns (port, api_key)."""
    servers = []

    def _start(name, api_key):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(name, api_key))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield _start
    for server in servers:
        server.shutdown()


@pytest.fixture
def client(tmp_path, monkeypatch, model_server):
    store = Store(path=tmp_path / "e2e.db")
    monkeypatch.setattr(central_app, "store", store)
    monkeypatch.setattr(central_app, "_reconcile_stale", lambda *a, **kw: None)

    async def _noop(deploy_id):
        return None

    monkeypatch.setattr(central_app, "_deploy_job", _noop)

    with TestClient(central_app.app) as c:
        c.store = store
        c.model_server = model_server
        token = c.post("/auth/register",
                       json={"email": "e2e@x.com", "password": "hunter2hunter2"}).json()["token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _deploy(client, name):
    """A deploy wired to a live fake model on its allocated port."""
    body = client.post("/deploys", json={"model": name, "runtime": "llama"}).json()
    port = client.model_server(name, body["spec"]["api_key"])
    deploy = client.store.get(body["id"])
    deploy.status = DeployState.HEALTHY
    deploy.endpoint = f"http://127.0.0.1:{port}/v1"
    client.store.update(deploy)
    return body["id"]


@pytest.fixture
def pool(client):
    ids = [_deploy(client, "small"), _deploy(client, "large")]
    resp = client.post("/pools", json={"name": "p", "model_ids": ids, "mode": "stage"})
    assert resp.status_code == 201, resp.text
    return {"id": resp.json()["id"], "ids": ids}


def _set_mode(client, pool, mode):
    resp = client.put(f"/pools/{pool['id']}",
                      json={"name": "p", "model_ids": pool["ids"], "mode": mode})
    assert resp.status_code == 200, resp.text


def _stream(client, **body):
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "router", "stream": True, **body}) as resp:
        assert resp.status_code == 200
        events = [line[6:] for line in resp.iter_lines() if line.startswith("data: ")]
    return events


def _text(events):
    out = ""
    for e in events:
        if e == "[DONE]":
            continue
        delta = json.loads(e)["choices"][0].get("delta", {})
        out += delta.get("content", "")
    return out


def _chunks(events):
    return [json.loads(e) for e in events if e != "[DONE]"]


# ---- non-streaming ----

def test_the_answer_comes_from_a_real_model_server(client, pool):
    body = client.post("/v1/chat/completions",
                       json={"model": "router",
                             "messages": [{"role": "user", "content": "oi"}]}).json()
    assert body["choices"][0]["message"]["content"] == _full_text("small")


def test_the_response_names_the_model_that_answered(client, pool):
    body = client.post("/v1/chat/completions",
                       json={"model": "router",
                             "messages": [{"role": "user", "content": "oi"}]}).json()
    assert "small" in body["model"]
    assert body["sursumai"]["served_model"] == "small"
    assert body["sursumai"]["served_deploy"] == pool["ids"][0]


def test_a_technical_question_reaches_the_strong_model(client, pool):
    body = client.post("/v1/chat/completions",
                       json={"model": "router",
                             "messages": [{"role": "user", "content": "prove this theorem"}]}).json()
    assert body["sursumai"]["served_model"] == "large"


def test_a_wrong_api_key_is_not_silently_accepted(client, pool):
    """If the per-deploy key stopped being sent, the model would answer 401 —
    this test fails loudly instead of the router looking fine."""
    deploy = client.store.get(pool["ids"][0])
    deploy.spec.api_key = "sk-sursum-wrong"
    client.store.update(deploy)
    resp = client.post("/v1/chat/completions",
                       json={"model": "router", "messages": [{"role": "user", "content": "oi"}]})
    assert resp.status_code == 502


# ---- streaming, decided up front ----

def test_the_stream_reassembles_the_answer_exactly_once(client, pool):
    events = _stream(client, messages=[{"role": "user", "content": "oi"}])
    assert _text(events) == _full_text("small")


def test_the_stream_really_streams(client, pool):
    events = _stream(client, messages=[{"role": "user", "content": "oi"}])
    with_content = [c for c in _chunks(events)
                    if c["choices"][0].get("delta", {}).get("content")]
    assert len(with_content) == TOKENS


def test_the_stream_ends_with_done(client, pool):
    assert _stream(client, messages=[{"role": "user", "content": "oi"}])[-1] == "[DONE]"


def test_every_streamed_chunk_names_the_served_model(client, pool):
    events = _stream(client, messages=[{"role": "user", "content": "oi"}])
    assert all("small" in c["model"] for c in _chunks(events))


def test_streamed_chunks_carry_the_session(client, pool):
    events = _stream(client, session_id="sess-1",
                     messages=[{"role": "user", "content": "oi"}])
    assert all(c.get("session_id") == "sess-1" for c in _chunks(events))


def test_the_strong_model_streams_too(client, pool):
    events = _stream(client, messages=[{"role": "user", "content": "prove this theorem"}])
    assert _text(events) == _full_text("large")


# ---- streaming, replayed (escalation had to read a reply first) ----

def test_the_replayed_stream_reassembles_exactly_once(client, pool):
    _set_mode(client, pool, "escalation")
    events = _stream(client, session_id="esc", messages=[{"role": "user", "content": "oi"}])
    assert _text(events) == _full_text("small")


def test_the_replayed_stream_is_chunked(client, pool):
    _set_mode(client, pool, "escalation")
    events = _stream(client, session_id="esc2", messages=[{"role": "user", "content": "oi"}])
    with_content = [c for c in _chunks(events)
                    if c["choices"][0].get("delta", {}).get("content")]
    assert len(with_content) > 1


def test_the_replayed_stream_reports_usage_at_the_end(client, pool):
    _set_mode(client, pool, "escalation")
    events = _stream(client, session_id="esc3", messages=[{"role": "user", "content": "oi"}])
    final = _chunks(events)[-1]
    assert final["usage"]["total_tokens"] == TOKENS
    assert final["sursumai"]["served_model"] == "small"


# ---- pools of N ----

def test_round_robin_alternates_between_the_models(client, pool):
    _set_mode(client, pool, "round_robin")
    served = []
    for _ in range(4):
        body = client.post("/v1/chat/completions",
                           json={"model": "router", "session_id": "rr",
                                 "messages": [{"role": "user", "content": "hi"}]}).json()
        served.append(body["sursumai"]["served_model"])
    assert served == ["small", "large", "small", "large"]


def test_round_robin_walks_a_pool_of_three(client):
    ids = [_deploy(client, "small"), _deploy(client, "mid"), _deploy(client, "large")]
    client.post("/pools", json={"name": "p3", "model_ids": ids, "mode": "round_robin"})
    served = []
    for _ in range(6):
        body = client.post("/v1/chat/completions",
                           json={"model": "router", "session_id": "rr3",
                                 "messages": [{"role": "user", "content": "hi"}]}).json()
        served.append(body["sursumai"]["served_model"])
    assert served == ["small", "mid", "large", "small", "mid", "large"]


def test_escalation_in_a_pool_of_three_uses_the_last_as_strong(client):
    ids = [_deploy(client, "small"), _deploy(client, "mid"), _deploy(client, "large")]
    client.post("/pools", json={"name": "p3", "model_ids": ids, "mode": "stage"})
    body = client.post("/v1/chat/completions",
                       json={"model": "router",
                             "messages": [{"role": "user", "content": "prove this theorem"}]}).json()
    assert body["sursumai"]["served_model"] == "large"


# ---- the decision log ----

def test_every_turn_is_logged_with_the_model_that_served_it(client, pool):
    client.post("/v1/chat/completions",
                json={"model": "router", "messages": [{"role": "user", "content": "oi"}]})
    log = client.get(f"/pools/{pool['id']}/log").json()
    assert log and log[-1]["model_served"] == pool["ids"][0]


# ---- the playground proxy ----

def test_the_playground_reaches_the_model_with_its_key(client, pool):
    body = client.post(f"/deploys/{pool['ids'][0]}/chat",
                       json={"messages": [{"role": "user", "content": "hi"}]}).json()
    assert body["choices"][0]["message"]["content"] == _full_text("small")
