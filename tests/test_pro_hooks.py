"""The seams the Pro module plugs into.

The free edition knows a deploy *could* live on another agent, and refuses it
in the API — not only by hiding a tab. With the module, the same code asks the
module's hooks where that agent is.
"""
import asyncio

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from central import app as central_app  # noqa: E402
from central.db import DeployState, Store  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = Store(path=tmp_path / "api.db")
    monkeypatch.setattr(central_app, "store", store)
    monkeypatch.setattr(central_app, "_reconcile_stale", lambda *a, **kw: None)
    run_job = central_app._deploy_job

    async def _no_background_job(deploy_id):
        return None

    monkeypatch.setattr(central_app, "_deploy_job", _no_background_job)
    hooks = central_app.ProHooks()
    hooks.startup, hooks.shutdown = [], []
    monkeypatch.setattr(central_app, "pro_hooks", hooks)
    with fastapi_testclient.TestClient(central_app.app) as c:
        c.store, c.hooks, c.run_job = store, hooks, run_job
        yield c


def _token(client):
    r = client.post("/auth/register", json={"email": "a@b.com", "password": "hunter2hunter2"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_the_free_edition_refuses_another_machine_in_the_api(client):
    h = _token(client)
    resp = client.post("/deploys", json={"model": "org/m", "runtime": "llama",
                                         "machine_id": "m1"}, headers=h)
    assert resp.status_code == 402
    assert "SursumAI Pro" in resp.json()["detail"]


def test_a_deploy_here_is_untouched_by_all_of_this(client):
    h = _token(client)
    resp = client.post("/deploys", json={"model": "org/m", "runtime": "llama"}, headers=h)
    assert resp.status_code == 201
    assert resp.json()["machine_id"] is None


def test_with_the_module_the_deploy_goes_to_the_agent_it_names(client, monkeypatch):
    seen = []
    client.hooks.check_target = lambda machine_id, user: None
    client.hooks.agent_url = lambda machine_id: f"http://127.0.0.1:18000/{machine_id}"
    client.hooks.reachable_endpoint = lambda machine_id, ep: "http://127.0.0.1:18001/v1"
    ac = central_app.agent_client

    def _rec(result):
        def _f(*args, **kwargs):
            seen.append(args[-1])
            return result
        return _f

    monkeypatch.setattr(ac, "preflight", _rec({"checks": []}))
    monkeypatch.setattr(ac, "start", _rec({}))
    monkeypatch.setattr(ac, "status", _rec({"healthy": True, "running": True,
                                             "endpoint": "http://localhost:9000/v1"}))
    h = _token(client)
    did = client.post("/deploys", json={"model": "org/m", "runtime": "llama",
                                        "machine_id": "m1"}, headers=h).json()["id"]
    asyncio.run(client.run_job(did))

    raw = client.store._conn.execute("SELECT endpoint FROM deploys WHERE id=?", (did,)).fetchone()
    assert raw["endpoint"] == "http://localhost:9000/v1"      # stored as the agent sees it
    assert all(agent == "http://127.0.0.1:18000/m1" for agent in seen), seen


def test_a_deploy_elsewhere_without_the_module_says_why_not_just_fails(client):
    """An install that lost its Pro module still has the rows; it must say
    what is missing, not 'agent unreachable'."""
    h = _token(client)
    deploy = client.store.create(
        central_app.Spec(model="org/m", runtime="llama"), "someone", machine_id="m1")
    deploy.status = DeployState.HEALTHY
    client.store.update(deploy)
    assert "SursumAI Pro" in central_app._no_endpoint(client.store.get(deploy.id))
