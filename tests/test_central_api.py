"""The central's HTTP surface: auth, ownership and pool validation.

The agent is faked, so no model is ever started. What matters here is that one
user can never see or touch another user's deployments.
"""

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from central import app as central_app  # noqa: E402
from central.db import DeployState, Store  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A central with its own database and an agent that never runs anything."""
    store = Store(path=tmp_path / "api.db")
    monkeypatch.setattr(central_app, "store", store)
    monkeypatch.setattr(central_app.router_mod, "Store", Store, raising=False)

    async def _noop_job(deploy_id):
        return None

    monkeypatch.setattr(central_app, "_deploy_job", _noop_job)
    monkeypatch.setattr(central_app, "_reconcile_stale", lambda *a, **kw: None)
    with TestClient(central_app.app) as c:
        c.store = store
        yield c


def _register(client, email="a@b.com", password="hunter2hunter2"):
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _make_deploy(client, token, model="org/m", healthy=True):
    resp = client.post("/deploys", json={"model": model, "runtime": "llama"},
                       headers=_auth(token))
    assert resp.status_code == 201, resp.text
    deploy_id = resp.json()["id"]
    if healthy:
        deploy = client.store.get(deploy_id)
        deploy.status = DeployState.HEALTHY
        deploy.endpoint = f"http://localhost:{deploy.spec.port}/v1"
        client.store.update(deploy)
    return deploy_id


# ---- registration and login ----

def test_register_returns_a_working_token(client):
    token = _register(client)
    assert client.get("/auth/me", headers=_auth(token)).json()["email"] == "a@b.com"


def test_email_is_normalised(client):
    _register(client, email="A@B.com")
    assert client.post("/auth/login",
                       json={"email": "a@b.com", "password": "hunter2hunter2"}).status_code == 200


@pytest.mark.parametrize("email", ["nope", "a@b", "@b.com", "a b@c.com"])
def test_invalid_emails_are_refused(client, email):
    resp = client.post("/auth/register", json={"email": email, "password": "hunter2hunter2"})
    assert resp.status_code == 422


def test_short_passwords_are_refused(client):
    resp = client.post("/auth/register", json={"email": "a@b.com", "password": "short"})
    assert resp.status_code == 422


def test_an_email_cannot_register_twice(client):
    _register(client)
    resp = client.post("/auth/register", json={"email": "a@b.com", "password": "hunter2hunter2"})
    assert resp.status_code == 409


def test_a_wrong_password_is_rejected(client):
    _register(client)
    resp = client.post("/auth/login", json={"email": "a@b.com", "password": "wrongwrongwrong"})
    assert resp.status_code == 401


def test_logout_invalidates_the_token(client):
    token = _register(client)
    client.post("/auth/logout", headers=_auth(token))
    assert client.get("/auth/me", headers=_auth(token)).status_code == 401


def test_the_plaintext_token_is_never_stored(client):
    token = _register(client)
    rows = client.store._conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert rows and all(r["token_hash"] != token for r in rows)


# ---- everything else needs a token ----

@pytest.mark.parametrize("method,path", [
    ("GET", "/deploys"),
    ("POST", "/deploys"),
    ("GET", "/deploys/x"),
    ("DELETE", "/deploys/x"),
    ("GET", "/deploys/x/logs"),
    ("GET", "/deploys/x/metrics"),
    ("POST", "/deploys/x/chat"),
    ("GET", "/pools"),
    ("POST", "/pools"),
    ("GET", "/auth/me"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/v1/models"),
])
def test_the_api_is_closed_without_a_token(client, method, path):
    assert client.request(method, path, json={}).status_code == 401


def test_a_made_up_token_is_rejected(client):
    assert client.get("/deploys", headers=_auth("not-a-real-token")).status_code == 401


# ---- ownership ----

def test_a_user_only_sees_their_own_deploys(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    _make_deploy(client, alice)

    assert len(client.get("/deploys", headers=_auth(alice)).json()) == 1
    assert client.get("/deploys", headers=_auth(bob)).json() == []


@pytest.mark.parametrize("method,suffix", [
    ("GET", ""), ("DELETE", ""), ("GET", "/logs"), ("GET", "/metrics"),
])
def test_another_users_deploy_is_not_found(client, method, suffix):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    deploy_id = _make_deploy(client, alice)
    resp = client.request(method, f"/deploys/{deploy_id}{suffix}", headers=_auth(bob))
    assert resp.status_code == 404


def test_another_users_deploy_cannot_be_chatted_with(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    deploy_id = _make_deploy(client, alice)
    resp = client.post(f"/deploys/{deploy_id}/chat",
                       json={"messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(bob))
    assert resp.status_code == 404


def test_a_pool_cannot_be_built_from_another_users_deploys(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    a, b = _make_deploy(client, alice), _make_deploy(client, alice)
    resp = client.post("/pools", json={"name": "p", "model_ids": [a, b]},
                       headers=_auth(bob))
    assert resp.status_code == 422


# ---- deploys ----

def test_a_new_deploy_gets_a_port_and_an_internal_key(client):
    token = _register(client)
    body = client.post("/deploys", json={"model": "org/m", "runtime": "llama"},
                       headers=_auth(token)).json()
    assert 9000 <= body["spec"]["port"] <= 9099
    # internal: it locks the model port, the user holds an account key instead
    assert body["spec"]["api_key"].startswith("sk-internal-")


def test_deploys_never_share_a_port(client):
    token = _register(client)
    seen = {client.post("/deploys", json={"model": "org/m", "runtime": "llama"},
                        headers=_auth(token)).json()["spec"]["port"] for _ in range(25)}
    assert len(seen) == 25


def test_an_invalid_spec_is_refused(client):
    token = _register(client)
    resp = client.post("/deploys", json={"model": "org/m", "runtime": "ollama"},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_the_hf_token_is_never_echoed_back(client):
    token = _register(client)
    body = client.post("/deploys",
                       json={"model": "org/m", "runtime": "llama", "hf_token": "hf_secret"},
                       headers=_auth(token)).json()
    assert body["spec"]["hf_token"] == "***"


def test_a_redeploy_keeps_the_port_and_the_key(client):
    token = _register(client)
    deploy_id = _make_deploy(client, token)
    before = client.get(f"/deploys/{deploy_id}", headers=_auth(token)).json()["spec"]
    after = client.post(f"/deploys/{deploy_id}/redeploy", json={"max_tokens": 99},
                        headers=_auth(token)).json()["spec"]
    assert after["port"] == before["port"]
    assert after["api_key"] == before["api_key"]
    assert after["max_tokens"] == 99


def test_destroying_a_deploy_removes_it(client):
    token = _register(client)
    deploy_id = _make_deploy(client, token)
    assert client.delete(f"/deploys/{deploy_id}", headers=_auth(token)).status_code == 200
    assert client.get(f"/deploys/{deploy_id}", headers=_auth(token)).status_code == 404


# ---- pools ----

def test_a_pool_needs_at_least_two_models(client):
    token = _register(client)
    a = _make_deploy(client, token)
    resp = client.post("/pools", json={"name": "p", "model_ids": [a]}, headers=_auth(token))
    assert resp.status_code == 422


def test_a_pool_rejects_the_same_model_twice(client):
    token = _register(client)
    a = _make_deploy(client, token)
    resp = client.post("/pools", json={"name": "p", "model_ids": [a, a]},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_a_pool_rejects_an_unhealthy_model(client):
    token = _register(client)
    a = _make_deploy(client, token)
    b = _make_deploy(client, token, healthy=False)
    resp = client.post("/pools", json={"name": "p", "model_ids": [a, b]},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_a_rejected_pool_is_not_left_behind(client):
    """The pool row is created before its members are checked — a failed
    check must roll it back, not leave a half-built pool in the list."""
    token = _register(client)
    a = _make_deploy(client, token)
    b = _make_deploy(client, token, healthy=False)
    client.post("/pools", json={"name": "p", "model_ids": [a, b]}, headers=_auth(token))
    assert client.get("/pools", headers=_auth(token)).json() == []


def test_pool_names_are_unique_per_user(client):
    token = _register(client)
    a, b = _make_deploy(client, token), _make_deploy(client, token)
    client.post("/pools", json={"name": "p", "model_ids": [a, b]}, headers=_auth(token))
    resp = client.post("/pools", json={"name": "p", "model_ids": [a, b]},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_an_unknown_pool_mode_is_refused(client):
    token = _register(client)
    a, b = _make_deploy(client, token), _make_deploy(client, token)
    resp = client.post("/pools", json={"name": "p", "model_ids": [a, b], "mode": "magic"},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_a_pool_keeps_every_model_it_was_given(client):
    """A pool of 4 must stay a pool of 4 — not collapse to weak/strong."""
    token = _register(client)
    ids = [_make_deploy(client, token, model=f"org/m{i}") for i in range(4)]
    body = client.post("/pools", json={"name": "p", "model_ids": ids, "mode": "classifier"},
                       headers=_auth(token)).json()
    assert body["model_ids"] == ids


def test_a_pool_can_be_renamed_and_remoded(client):
    token = _register(client)
    ids = [_make_deploy(client, token, model=f"org/m{i}") for i in range(3)]
    pool_id = client.post("/pools", json={"name": "p", "model_ids": ids},
                          headers=_auth(token)).json()["id"]
    body = client.put(f"/pools/{pool_id}",
                      json={"name": "renamed", "model_ids": ids, "mode": "stage"},
                      headers=_auth(token)).json()
    assert body["name"] == "renamed" and body["mode"] == "stage"


def test_another_users_pool_is_not_found(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    ids = [_make_deploy(client, alice), _make_deploy(client, alice)]
    pool_id = client.post("/pools", json={"name": "p", "model_ids": ids},
                          headers=_auth(alice)).json()["id"]
    assert client.delete(f"/pools/{pool_id}", headers=_auth(bob)).status_code == 404
    assert client.get(f"/pools/{pool_id}/log", headers=_auth(bob)).status_code == 404


# ---- router endpoint ----

def test_the_router_needs_a_pool(client):
    token = _register(client)
    resp = client.post("/v1/chat/completions",
                       json={"model": "router", "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_the_router_needs_messages(client):
    token = _register(client)
    resp = client.post("/v1/chat/completions", json={"model": "router", "messages": []},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_an_unknown_pool_is_not_found(client):
    token = _register(client)
    a, b = _make_deploy(client, token), _make_deploy(client, token)
    client.post("/pools", json={"name": "p", "model_ids": [a, b]}, headers=_auth(token))
    resp = client.post("/v1/chat/completions",
                       json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(token))
    assert resp.status_code == 404


def test_an_oversized_session_id_is_refused(client):
    token = _register(client)
    a, b = _make_deploy(client, token), _make_deploy(client, token)
    client.post("/pools", json={"name": "p", "model_ids": [a, b]}, headers=_auth(token))
    resp = client.post("/v1/chat/completions",
                       json={"model": "router", "session_id": "x" * 500,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(token))
    assert resp.status_code == 422


def test_models_lists_deployments_pools_and_the_router(client):
    token = _register(client)
    ids = [_make_deploy(client, token, model=f"org/m{i}") for i in range(3)]
    client.post("/pools", json={"name": "p", "model_ids": ids}, headers=_auth(token))
    data = client.get("/v1/models", headers=_auth(token)).json()["data"]
    listed = [d["id"] for d in data]
    # addressable by model name, like any OpenAI client expects
    assert {"org/m0", "org/m1", "org/m2"} <= set(listed)
    assert "p" in listed and "router" in listed
    pool_entry = next(d for d in data if d["id"] == "p")
    assert [m["model"] for m in pool_entry["models"]] == ["org/m0", "org/m1", "org/m2"]


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---- account API keys ----

def _api_key(client, token, name="test key"):
    resp = client.post("/api-keys", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_key_belongs_to_the_account_not_to_a_model(client):
    """One key, every deployment — the way OpenAI and Anthropic work."""
    token = _register(client)
    for i in range(3):
        _make_deploy(client, token, model=f"org/m{i}")
    key = _api_key(client, token)["key"]
    listed = client.get("/v1/models", headers=_auth(key)).json()["data"]
    assert {"org/m0", "org/m1", "org/m2"} <= {d["id"] for d in listed}


def test_you_can_hold_several_keys(client):
    token = _register(client)
    first, second = _api_key(client, token, "laptop"), _api_key(client, token, "ci")
    assert first["key"] != second["key"]
    assert {k["name"] for k in client.get("/api-keys", headers=_auth(token)).json()} == {
        "laptop", "ci"}


def test_the_plaintext_key_is_returned_once_and_never_stored(client):
    token = _register(client)
    key = _api_key(client, token)["key"]
    listed = client.get("/api-keys", headers=_auth(token)).json()
    assert all("key" not in k for k in listed)
    rows = client.store._conn.execute("SELECT key_hash, display FROM api_keys").fetchall()
    assert all(r["key_hash"] != key for r in rows)
    assert all(key not in r["display"] for r in rows)


def test_a_listed_key_is_recognisable_without_being_readable(client):
    token = _register(client)
    key = _api_key(client, token)["key"]
    shown = client.get("/api-keys", headers=_auth(token)).json()[0]["display"]
    assert shown.startswith("sk-sursum-") and shown.endswith(key[-4:])
    assert shown != key


def test_a_revoked_key_stops_working(client):
    token = _register(client)
    created = _api_key(client, token)
    key = created["key"]
    assert client.get("/v1/models", headers=_auth(key)).status_code == 200
    assert client.delete(f"/api-keys/{created['id']}",
                         headers=_auth(token)).status_code == 200
    assert client.get("/v1/models", headers=_auth(key)).status_code == 401


def test_a_revoked_key_disappears_from_the_list(client):
    token = _register(client)
    created = _api_key(client, token)
    client.delete(f"/api-keys/{created['id']}", headers=_auth(token))
    assert client.get("/api-keys", headers=_auth(token)).json() == []


def test_a_made_up_key_is_rejected(client):
    _register(client)
    assert client.get("/v1/models",
                      headers=_auth("sk-sursum-nonsense")).status_code == 401


def test_another_users_key_cannot_be_revoked(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    created = _api_key(client, alice)
    assert client.delete(f"/api-keys/{created['id']}",
                         headers=_auth(bob)).status_code == 404


def test_a_key_sees_only_its_own_account(client):
    alice, bob = _register(client, "alice@x.com"), _register(client, "bob@x.com")
    _make_deploy(client, alice, model="alice/model")
    bob_key = _api_key(client, bob)["key"]
    listed = client.get("/v1/models", headers=_auth(bob_key)).json()["data"]
    assert "alice/model" not in {d["id"] for d in listed}


# ---- what a key may do ----

@pytest.mark.parametrize("method,path", [
    ("GET", "/deploys"),
    ("POST", "/deploys"),
    ("DELETE", "/deploys/x"),
    ("GET", "/api-keys"),
    ("POST", "/api-keys"),
    ("POST", "/pools"),
])
def test_an_api_key_cannot_manage_the_account(client, method, path):
    """A key that leaks out of an inference script must not be able to destroy
    deployments or mint more keys."""
    token = _register(client)
    key = _api_key(client, token)["key"]
    assert client.request(method, path, json={}, headers=_auth(key)).status_code == 403


def test_a_session_still_works_for_inference(client):
    """The browser has no API key; it uses the session it already has."""
    token = _register(client)
    _make_deploy(client, token, model="org/m")
    assert client.get("/v1/models", headers=_auth(token)).status_code == 200


# ---- addressing a model by name ----

def test_a_deployment_is_addressable_by_its_model_name(client, monkeypatch):
    token = _register(client)
    _make_deploy(client, token, model="Qwen/Qwen3-0.6B-GGUF")
    key = _api_key(client, token)["key"]

    seen = {}

    def _chat(endpoint, payload, timeout=180.0, api_key=None):
        seen.update(endpoint=endpoint, api_key=api_key, model=payload["model"])
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"total_tokens": 2}}

    monkeypatch.setattr(central_app.agent_client, "chat", _chat)
    body = client.post("/v1/chat/completions",
                       json={"model": "Qwen/Qwen3-0.6B-GGUF",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(key)).json()
    assert body["choices"][0]["message"]["content"] == "hi"
    assert seen["model"] == "Qwen/Qwen3-0.6B-GGUF"
    # the deployment's own key is internal and added by the central
    assert seen["api_key"].startswith("sk-internal-")


def test_an_unknown_model_says_what_is_available(client):
    token = _register(client)
    _make_deploy(client, token, model="org/real")
    key = _api_key(client, token)["key"]
    resp = client.post("/v1/chat/completions",
                       json={"model": "org/nope",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(key))
    assert resp.status_code == 404
    assert "org/real" in resp.json()["detail"]


def test_a_model_that_is_not_ready_says_so(client):
    token = _register(client)
    _make_deploy(client, token, model="org/slow", healthy=False)
    key = _api_key(client, token)["key"]
    resp = client.post("/v1/chat/completions",
                       json={"model": "org/slow",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(key))
    assert resp.status_code == 422
    assert "not ready" in resp.json()["detail"]


def test_a_pool_is_addressable_by_its_name(client, monkeypatch):
    token = _register(client)
    ids = [_make_deploy(client, token, model=f"org/m{i}") for i in range(2)]
    client.post("/pools", json={"name": "meu-pool", "model_ids": ids},
                headers=_auth(token))
    key = _api_key(client, token)["key"]

    monkeypatch.setattr(central_app.router_mod, "route_turn",
                        lambda *a, **kw: {"served": ids[0], "served_model": "org/m0",
                                          "served_endpoint": "", "decision": "weak_ok",
                                          "content": "pooled", "reasoning": "",
                                          "usage": {"total_tokens": 1}, "latched": False})
    body = client.post("/v1/chat/completions",
                       json={"model": "meu-pool",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth(key)).json()
    assert body["choices"][0]["message"]["content"] == "pooled"
