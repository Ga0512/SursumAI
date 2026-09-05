"""The agent<->central contract.

The central talks to the agent through `central.agent_client`, which builds
plain urllib requests. These tests capture the request the client would send
and replay it into the real agent app, so a change on either side that breaks
the other fails here — no network, no docker, no model.
"""

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from agent import app as agent_app  # noqa: E402
from central import agent_client  # noqa: E402
from core.spec import Spec  # noqa: E402

SPEC = Spec(model="org/model", runtime="llama", api_key="sk-sursum-test").to_dict()
DEPLOY_ID = "deadbeefcafe"


@pytest.fixture
def client():
    return TestClient(agent_app.app)


@pytest.fixture
def capture(monkeypatch):
    """Run an agent_client call without a network; return the request it built."""
    sent = {}

    class _FakeResponse:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        sent["method"] = req.get_method()
        sent["url"] = req.full_url
        sent["headers"] = {k.lower(): v for k, v in req.header_items()}
        sent["body"] = json.loads(req.data.decode()) if req.data else None
        return _FakeResponse()

    monkeypatch.setattr(agent_client.urllib.request, "urlopen", _fake_urlopen)
    return sent


def _replay(client, sent, key="test-agent-key"):
    """Send the captured request into the agent app."""
    path = sent["url"].split("8010", 1)[1]
    headers = {"X-Agent-Key": key}
    return client.request(sent["method"], path, json=sent["body"], headers=headers)


# ---- the key ----

@pytest.mark.parametrize("method,path", [
    ("POST", "/preflight"),
    ("POST", "/deploys"),
    ("GET", f"/deploys/{DEPLOY_ID}/status"),
    ("GET", f"/deploys/{DEPLOY_ID}/logs"),
    ("GET", f"/deploys/{DEPLOY_ID}/metrics"),
    ("POST", f"/deploys/{DEPLOY_ID}/stop"),
])
def test_every_agent_endpoint_requires_the_key(client, method, path):
    assert client.request(method, path, json={}).status_code == 401
    assert client.request(method, path, json={},
                          headers={"X-Agent-Key": "wrong"}).status_code == 401


def test_health_is_the_only_open_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_the_client_sends_the_key_header(capture):
    agent_client.status(DEPLOY_ID)
    assert capture["headers"]["x-agent-key"] == "test-agent-key"


# ---- shapes both sides agree on ----

def test_start_request_shape(client, capture, monkeypatch):
    monkeypatch.setattr(agent_app.executor_llama, "start", lambda spec, did: "ok")
    agent_client.start(DEPLOY_ID, SPEC)
    assert capture["body"] == {"deploy_id": DEPLOY_ID, "spec": SPEC}

    resp = _replay(client, capture)
    assert resp.status_code == 202
    assert resp.json() == {"deploy_id": DEPLOY_ID, "status": "started"}


def test_preflight_request_shape(client, capture, monkeypatch):
    checks = [{"name": "model", "ok": True, "detail": "found"}]
    monkeypatch.setattr(agent_app.executor_llama, "preflight", lambda spec: checks)
    agent_client.preflight(SPEC)
    assert capture["body"] == {"spec": SPEC}

    body = _replay(client, capture).json()
    # the central reads exactly these fields in _run_preflight/_deploy_job
    assert body["runtime"] == "llama"
    assert all({"name", "ok", "detail"} <= set(c) for c in body["checks"])


def test_an_invalid_spec_is_rejected_before_anything_runs(client, capture):
    agent_client.preflight({"model": "org/m", "runtime": "ollama"})
    assert _replay(client, capture).status_code == 422


def test_status_shape(client, capture, monkeypatch):
    monkeypatch.setattr(agent_app.executor, "is_running", lambda did: False)
    monkeypatch.setattr(agent_app.executor_llama, "is_running", lambda did: True)
    monkeypatch.setattr(agent_app.executor_llama, "stage", lambda did: "starting")
    monkeypatch.setattr(agent_app, "_probe_healthy", lambda ep, key=None: True)
    monkeypatch.setattr(agent_app, "_auth_enforced", lambda ep, key: True)

    agent_client.status(DEPLOY_ID)
    assert capture["method"] == "GET"

    body = _replay(client, capture).json()
    # _wait_healthy, _reconcile_stale and get_deploy read these four
    assert body["running"] is True and body["healthy"] is True
    assert body["endpoint"].startswith("http://localhost:9")
    assert body["stage"] == "starting"


def test_status_carries_the_deploy_api_key_to_the_probe(client, monkeypatch):
    """A deploy started with --api-key must be probed with that bearer, or the
    central would mark a perfectly healthy deploy as failed."""
    seen = {}
    monkeypatch.setattr(agent_app.executor, "is_running", lambda did: True)
    monkeypatch.setattr(agent_app.executor_llama, "is_running", lambda did: False)
    monkeypatch.setattr(agent_app.executor, "stage", lambda did: "running")
    monkeypatch.setattr(agent_app, "_probe_healthy",
                        lambda ep, key=None: seen.setdefault("key", key) or True)
    agent_app.SPECS[DEPLOY_ID] = Spec.from_dict(SPEC)

    client.get(f"/deploys/{DEPLOY_ID}/status", headers={"X-Agent-Key": "test-agent-key"})
    assert seen["key"] == "sk-sursum-test"
    agent_app.SPECS.pop(DEPLOY_ID, None)


def test_logs_shape(client, capture, monkeypatch):
    monkeypatch.setattr(agent_app.executor_llama, "logs",
                        lambda did, tail=300: "line one\nline two\n")
    agent_client.logs(DEPLOY_ID, tail=42)
    assert "tail=42" in capture["url"]
    assert "line one" in _replay(client, capture).json()["logs"]


def test_metrics_404_when_nothing_is_running(client, capture, monkeypatch):
    monkeypatch.setattr(agent_app.executor, "is_running", lambda did: False)
    monkeypatch.setattr(agent_app.executor_llama, "is_running", lambda did: False)
    agent_client.metrics(DEPLOY_ID)
    assert _replay(client, capture).status_code == 404


def test_stop_shape(client, capture, monkeypatch):
    monkeypatch.setattr(agent_app.executor, "stop", lambda did: None)
    monkeypatch.setattr(agent_app.executor_llama, "stop", lambda did: None)
    agent_client.stop(DEPLOY_ID)
    assert _replay(client, capture).json() == {"deploy_id": DEPLOY_ID, "status": "stopped"}


def test_agent_errors_reach_the_central_as_agent_error(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(agent_client.urllib.request, "urlopen", _boom)
    with pytest.raises(agent_client.AgentError):
        agent_client.status(DEPLOY_ID)


# ---- the runtime receives the deploy key, without it reaching argv ----

def test_vllm_receives_the_key_through_the_environment():
    from agent import executor
    spec = Spec.from_dict(SPEC)
    cmd = executor.build_cmd(spec, DEPLOY_ID)
    assert "sk-sursum-test" not in " ".join(cmd)
    assert executor.runtime_env(spec)["VLLM_API_KEY"] == "sk-sursum-test"


def test_llama_reads_its_key_from_a_file_not_from_argv():
    from agent import executor_llama
    spec = Spec.from_dict(SPEC)
    for cmd in (
        executor_llama._binary_build_cmd(spec, DEPLOY_ID, {"gguf": "/models/m.gguf"},
                                         "llama-server"),
        executor_llama._docker_build_cmd(spec, DEPLOY_ID, {"gguf": "/models/m.gguf"}),
    ):
        assert "sk-sursum-test" not in " ".join(cmd)
        assert "--api-key-file" in cmd
    executor_llama.stop(DEPLOY_ID)


def test_the_agent_reports_whether_auth_is_really_enforced(client, monkeypatch):
    """Trusting the flag is not enough: a runtime that ignored its key file
    would still answer the authenticated probe, and look perfectly healthy.

    Probed on /chat/completions rather than /models, because llama.cpp keeps
    the model list public on purpose — a 200 there proves nothing."""
    monkeypatch.setattr(agent_app.executor, "is_running", lambda did: True)
    monkeypatch.setattr(agent_app.executor_llama, "is_running", lambda did: False)
    monkeypatch.setattr(agent_app.executor, "stage", lambda did: "running")
    monkeypatch.setattr(agent_app, "_probe_healthy", lambda ep, key=None: True)
    agent_app.SPECS[DEPLOY_ID] = Spec.from_dict(SPEC)

    import urllib.error

    def _answers(status):
        """Fake the model endpoint answering an unauthenticated POST."""
        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _urlopen(req, timeout=None):
            if status < 400:
                return _Resp()
            raise urllib.error.HTTPError(req.full_url, status, "", None, None)

        return _urlopen

    # the endpoint answers an unauthenticated caller: the key is not applied
    monkeypatch.setattr(agent_app.urllib.request, "urlopen", _answers(200))
    body = client.get(f"/deploys/{DEPLOY_ID}/status",
                      headers={"X-Agent-Key": "test-agent-key"}).json()
    assert body["auth_enforced"] is False

    # the endpoint refuses it: the key is doing its job
    monkeypatch.setattr(agent_app.urllib.request, "urlopen", _answers(401))
    body = client.get(f"/deploys/{DEPLOY_ID}/status",
                      headers={"X-Agent-Key": "test-agent-key"}).json()
    assert body["auth_enforced"] is True

    agent_app.SPECS.pop(DEPLOY_ID, None)
