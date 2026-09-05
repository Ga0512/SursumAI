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

def test_a_new_deploy_gets_a_port_and_an_api_key(client):
    token = _register(client)
    body = client.post("/deploys", json={"model": "org/m", "runtime": "llama"},
                       headers=_auth(token)).json()
    assert 9000 <= body["spec"]["port"] <= 9099
    assert body["spec"]["api_key"].startswith("sk-sursum-")


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


def test_models_lists_the_pool_members(client):
    token = _register(client)
    ids = [_make_deploy(client, token, model=f"org/m{i}") for i in range(3)]
    client.post("/pools", json={"name": "p", "model_ids": ids}, headers=_auth(token))
    data = client.get("/v1/models", headers=_auth(token)).json()["data"]
    pool_entry = next(d for d in data if d["id"] != "router")
    assert [m["model"] for m in pool_entry["models"]] == ["org/m0", "org/m1", "org/m2"]


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}
