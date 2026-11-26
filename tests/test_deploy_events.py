"""The history a deploy never had.

A deploy carried one `error` column, overwritten on every transition, so "why
did this go failed at 3am and recover at 3:02" had no answer anywhere. The
router already kept every decision in `router_log` while the deploy -- the
more important object -- kept nothing. Logs do not fill the gap: they are the
runtime's output, they live on the machine, and they are now trimmed.
"""

import pytest

from central.db import DeployState, Store
from core.spec import Spec


def _store(tmp_path) -> Store:
    return Store(path=tmp_path / "events.db")


def _spec(model="org/m"):
    return Spec(model=model, runtime="llama")


# ---- what gets written ----

def test_a_deploy_has_a_history_from_birth(tmp_path):
    """The first row answers "when was this asked for", not only "when did it
    break"."""
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    events = store.list_events(deploy.id)
    assert len(events) == 1
    assert events[0]["status"] == deploy.status
    assert events[0]["at"]


def test_every_transition_is_recorded_in_order(tmp_path):
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    for state in (DeployState.PROVISIONING, DeployState.HEALTHY):
        deploy.status = state
        store.update(deploy)
    assert [e["status"] for e in store.list_events(deploy.id)][:2] == [
        DeployState.HEALTHY, DeployState.PROVISIONING], "newest first"


def test_the_failure_reason_is_kept_next_to_the_status(tmp_path):
    """The sentence `_why_it_died()` produced is the whole value of the row;
    without it this is a status log that explains nothing."""
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    deploy.status = DeployState.FAILED
    deploy.error = "this model was not published quantized that way"
    store.update(deploy)
    newest = store.list_events(deploy.id)[0]
    assert newest["status"] == DeployState.FAILED
    assert "quantized" in newest["error"]


def test_an_earlier_reason_survives_a_later_one(tmp_path):
    """The bug this table exists for: the deploy row keeps only the last
    error, so a deploy that failed, was redeployed and failed differently
    erased the first reason -- which is usually the one that explains the
    second."""
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    deploy.status, deploy.error = DeployState.FAILED, "out of memory"
    store.update(deploy)
    deploy.status, deploy.error = DeployState.FAILED, "port already in use"
    store.update(deploy)

    assert store.get(deploy.id).error == "port already in use", "the row holds the last one"
    reasons = [e["error"] for e in store.list_events(deploy.id)]
    assert "out of memory" in reasons, "the first reason was lost"
    assert "port already in use" in reasons


def test_an_update_that_changes_nothing_writes_nothing(tmp_path):
    """`update` is also called by the reconcile pass. One row per pass would
    bury the real transitions and grow the table for nothing."""
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    deploy.status = DeployState.HEALTHY
    store.update(deploy)
    before = len(store.list_events(deploy.id))
    for _ in range(10):
        store.update(deploy)
    assert len(store.list_events(deploy.id)) == before


def test_histories_do_not_bleed_between_deploys(tmp_path):
    store = _store(tmp_path)
    a = store.create(_spec("org/a"), "u1")
    b = store.create(_spec("org/b"), "u1")
    a.status = DeployState.FAILED
    store.update(a)
    assert len(store.list_events(b.id)) == 1
    assert all(e["status"] != DeployState.FAILED for e in store.list_events(b.id))


# ---- it does not grow forever ----

def test_the_history_is_pruned(tmp_path):
    """The lesson from `metrics`, which grew without bound until every
    dashboard poll got slower."""
    store = _store(tmp_path)
    store.EVENTS_KEEP = 10
    deploy = store.create(_spec(), "u1")
    for i in range(40):
        deploy.error = f"reason {i}"
        store.update(deploy)
    events = store.list_events(deploy.id, limit=500)
    assert len(events) == 10
    assert events[0]["error"] == "reason 39", "pruning kept the wrong end"


def test_deleting_a_deploy_takes_its_history(tmp_path):
    store = _store(tmp_path)
    deploy = store.create(_spec(), "u1")
    deploy.status = DeployState.HEALTHY
    store.update(deploy)
    store.delete(deploy.id)
    assert store.list_events(deploy.id) == []


# ---- the route ----

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from central import app as central_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = Store(path=tmp_path / "api.db")
    monkeypatch.setattr(central_app, "store", store)

    async def _noop_job(deploy_id):
        return None

    monkeypatch.setattr(central_app, "_deploy_job", _noop_job)
    monkeypatch.setattr(central_app, "_reconcile_stale", lambda *a, **kw: None)
    with TestClient(central_app.app) as c:
        c.store = store
        yield c


def _token(client, email="a@b.com"):
    resp = client.post("/auth/register", json={"email": email, "password": "hunter2hunter2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _head(token):
    return {"Authorization": f"Bearer {token}"}


def test_the_route_returns_the_history(client):
    token = _token(client)
    created = client.post("/deploys", json={"model": "org/m", "runtime": "llama"},
                          headers=_head(token))
    assert created.status_code == 201, created.text
    deploy_id = created.json()["id"]
    resp = client.get(f"/deploys/{deploy_id}/events", headers=_head(token))
    assert resp.status_code == 200
    assert resp.json()["events"], "a deploy that exists has at least its creation"


def test_another_account_cannot_read_a_history(client):
    """The history names models and failure reasons: it is as private as the
    deploy itself."""
    alice = _token(client, "alice@b.com")
    bob = _token(client, "bob@b.com")
    created = client.post("/deploys", json={"model": "org/secret", "runtime": "llama"},
                          headers=_head(alice))
    deploy_id = created.json()["id"]
    assert client.get(f"/deploys/{deploy_id}/events",
                      headers=_head(bob)).status_code == 404


def test_the_history_needs_a_session_not_an_api_key(client):
    """Same rule as the rest of management: a key that leaked out of an
    inference script must not read the account's operational history."""
    token = _token(client)
    created = client.post("/deploys", json={"model": "org/m", "runtime": "llama"},
                          headers=_head(token))
    deploy_id = created.json()["id"]
    key = client.post("/api-keys", json={"name": "k"}, headers=_head(token)).json()["key"]
    assert client.get(f"/deploys/{deploy_id}/events",
                      headers=_head(key)).status_code == 403
