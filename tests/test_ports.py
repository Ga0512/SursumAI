"""Deploy port allocation.

The bug this replaces: the port was sha256(deploy_id) % 100, so two deploys
collided about half the time by the twelfth one, and nothing checked.
"""

import socket

import pytest

from central.db import Store
from core import ports
from core.spec import Spec


@pytest.fixture
def store(tmp_path):
    return Store(path=tmp_path / "test.db")


def _spec(model="org/m", **kw):
    return Spec(model=model, **kw)


# ---- the range ----

def test_range_is_9000_to_9099():
    assert ports.PORT_MIN == 9000 and ports.PORT_MAX == 9099
    assert len(ports.PORT_RANGE) == 100


@pytest.mark.parametrize("port,expected", [
    (9000, True), (9099, True), (9050, True),
    (8999, False), (9100, False), (None, False), (0, False),
])
def test_in_range(port, expected):
    assert ports.in_range(port) is expected


def test_first_free_takes_the_lowest_gap():
    assert ports.first_free({9000, 9001, 9003}) == 9002


def test_first_free_raises_when_the_range_is_full():
    with pytest.raises(ports.NoPortAvailable):
        ports.first_free(set(ports.PORT_RANGE))


def test_legacy_port_is_stable_and_in_range():
    for deploy_id in ("abc", "deadbeef", "x" * 32):
        assert ports.legacy_port(deploy_id) == ports.legacy_port(deploy_id)
        assert ports.in_range(ports.legacy_port(deploy_id))


def test_is_free_sees_a_listening_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        assert ports.is_free(busy) is False
    # released once the socket is closed
    assert ports.is_free(busy) is True


# ---- allocation ----

def test_each_deploy_gets_a_different_port(store):
    seen = {store.create(_spec(), "user-1").spec.port for _ in range(30)}
    assert len(seen) == 30
    assert all(ports.in_range(p) for p in seen)


def test_ports_are_allocated_lowest_first(store):
    assert store.create(_spec(), "u").spec.port == 9000
    assert store.create(_spec(), "u").spec.port == 9001


def test_a_destroyed_deploy_frees_its_port(store):
    first = store.create(_spec(), "u")
    store.create(_spec(), "u")
    store.delete(first.id)
    assert store.create(_spec(), "u").spec.port == first.spec.port


def test_ports_do_not_collide_across_users(store):
    a = store.create(_spec(), "user-a")
    b = store.create(_spec(), "user-b")
    assert a.spec.port != b.spec.port


def test_an_explicit_port_is_honoured(store):
    assert store.create(_spec(port=9042), "u").spec.port == 9042


def test_the_database_refuses_two_deploys_on_one_port(store):
    store.create(_spec(port=9042), "u")
    second = store.create(_spec(port=9042), "u")
    # the collision is caught and a free port handed out instead
    assert second.spec.port != 9042


def test_a_full_range_is_reported_not_silently_reused(store):
    for _ in range(100):
        store.create(_spec(), "u")
    with pytest.raises(ports.NoPortAvailable):
        store.create(_spec(), "u")


def test_allocation_is_safe_under_concurrent_creates(store):
    """Two requests arriving together must not be handed the same port."""
    import threading

    results, errors = [], []

    def _create():
        try:
            results.append(store.create(_spec(), "u").spec.port)
        except Exception as e:  # pragma: no cover - only on a regression
            errors.append(e)

    threads = [threading.Thread(target=_create) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(set(results)) == len(results) == 20


# ---- legacy deploys ----

def test_a_legacy_deploy_keeps_its_hashed_port(store):
    """Rows written before allocation existed have port NULL and must keep
    answering where they are actually listening."""
    deploy = store.create(_spec(), "u")
    store._conn.execute("UPDATE deploys SET port = NULL WHERE id = ?", (deploy.id,))
    store._conn.commit()
    assert ports.legacy_port(deploy.id) in store.taken_ports()


def test_a_new_deploy_never_lands_on_a_legacy_port(store):
    deploy = store.create(_spec(), "u")
    store._conn.execute("UPDATE deploys SET port = NULL WHERE id = ?", (deploy.id,))
    store._conn.commit()
    legacy = ports.legacy_port(deploy.id)
    assert store.create(_spec(), "u").spec.port != legacy


# ---- the executors agree with the allocation ----

def test_the_executors_use_the_allocated_port():
    from agent import executor, executor_llama

    spec = _spec(port=9042)
    assert executor.deploy_port("any-id", spec) == 9042
    assert executor_llama.deploy_port("any-id", spec) == 9042
    assert executor_llama.endpoint("any-id", spec) == "http://localhost:9042/v1"


def test_the_executors_fall_back_to_the_legacy_port_without_a_spec():
    from agent import executor_llama

    assert executor_llama.deploy_port("abc") == ports.legacy_port("abc")


# ---- releasing a port takes a moment ----

def test_wait_until_free_returns_at_once_for_a_free_port():
    import time as _time

    start = _time.monotonic()
    assert ports.wait_until_free(ports.PORT_MIN, timeout=5) is True
    assert _time.monotonic() - start < 1


def test_wait_until_free_gives_up_on_a_port_that_stays_busy():
    import time as _time

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy = srv.getsockname()[1]
        start = _time.monotonic()
        assert ports.wait_until_free(busy, timeout=1.5) is False
        assert _time.monotonic() - start >= 1.5


def test_wait_until_free_notices_a_port_being_released():
    """The real case: a container is still tearing down when the next deploy
    asks for its port."""
    import threading
    import time as _time

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    threading.Timer(1.0, srv.close).start()

    start = _time.monotonic()
    assert ports.wait_until_free(busy, timeout=8) is True
    assert _time.monotonic() - start >= 0.5
