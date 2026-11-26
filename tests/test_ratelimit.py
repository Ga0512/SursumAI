"""The ceiling per credential on /v1.

The account key was the only thing in front of the models and nothing counted
how often it was used, so a key that escaped into a script or a chat log was
free inference for whoever found it. The limit follows the exposure rather
than being a number picked for everyone: turning it on for every install
would have broken the workload this project documents as normal -- four
documents in parallel, a bench -- for a risk a loopback install does not have.
"""

import pytest

from central import ratelimit


# ---- when the limit exists at all ----

def test_loopback_has_no_limit(monkeypatch):
    monkeypatch.delenv("SURSUMAI_BIND", raising=False)
    monkeypatch.delenv(ratelimit.LIMIT_ENV, raising=False)
    assert ratelimit.configured_limit() == 0


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_an_explicit_loopback_bind_still_has_no_limit(monkeypatch, bind):
    monkeypatch.setenv("SURSUMAI_BIND", bind)
    monkeypatch.delenv(ratelimit.LIMIT_ENV, raising=False)
    assert ratelimit.configured_limit() == 0


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.247.4"])
def test_exposing_the_port_turns_the_limit_on(monkeypatch, bind):
    """The whole point: the limit appears exactly when the key stops being
    the only thing standing between a stranger and the GPU."""
    monkeypatch.setenv("SURSUMAI_BIND", bind)
    monkeypatch.delenv(ratelimit.LIMIT_ENV, raising=False)
    assert ratelimit.configured_limit() == ratelimit.EXPOSED_DEFAULT


def test_an_explicit_value_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("SURSUMAI_BIND", "0.0.0.0")
    monkeypatch.setenv(ratelimit.LIMIT_ENV, "10")
    assert ratelimit.configured_limit() == 10


def test_zero_turns_it_off_even_when_exposed(monkeypatch):
    """An operator who has a reverse proxy doing this must be able to say so."""
    monkeypatch.setenv("SURSUMAI_BIND", "0.0.0.0")
    monkeypatch.setenv(ratelimit.LIMIT_ENV, "0")
    assert ratelimit.configured_limit() == 0


def test_a_typo_does_not_remove_the_limit_on_an_exposed_port(monkeypatch):
    """Falling back to "no limit" would make a typo the most dangerous
    possible setting, and refusing to boot would make it the most annoying."""
    monkeypatch.setenv("SURSUMAI_BIND", "0.0.0.0")
    monkeypatch.setenv(ratelimit.LIMIT_ENV, "lots")
    assert ratelimit.configured_limit() == ratelimit.EXPOSED_DEFAULT


# ---- the window ----

def test_requests_under_the_limit_pass():
    lim = ratelimit.Limiter(limit=3)
    assert [lim.check("k", now=1.0) for _ in range(3)] == [0.0, 0.0, 0.0]


def test_the_request_over_the_limit_is_told_how_long_to_wait():
    lim = ratelimit.Limiter(limit=2, window=60.0)
    lim.check("k", now=10.0)
    lim.check("k", now=11.0)
    wait = lim.check("k", now=12.0)
    assert wait > 0
    # the oldest hit leaves the window at 10+60; from t=12 that is 58s
    assert 57.9 < wait < 58.1


def test_the_window_slides_instead_of_resetting():
    """A fixed minute lets twice the limit through across the boundary, which
    on a GPU is the difference between a queue that drains and one that does
    not."""
    lim = ratelimit.Limiter(limit=2, window=60.0)
    lim.check("k", now=0.0)
    lim.check("k", now=59.0)
    assert lim.check("k", now=59.5) > 0, "a third request inside the window passed"
    # 0.0 has now aged out, 59.0 has not: room for exactly one
    assert lim.check("k", now=60.5) == 0.0
    assert lim.check("k", now=60.6) > 0


def test_one_credential_does_not_spend_anothers_allowance():
    lim = ratelimit.Limiter(limit=1)
    assert lim.check("alice", now=1.0) == 0.0
    assert lim.check("bob", now=1.0) == 0.0, "buckets are shared between keys"
    assert lim.check("alice", now=1.0) > 0


def test_idle_identities_are_evicted():
    """A key is cheap to create, and this dict is unbounded otherwise."""
    lim = ratelimit.Limiter(limit=5, window=60.0)
    for i in range(5000):
        lim.check(f"key-{i}", now=1.0)
    before = len(lim._hits)
    lim.check("fresh", now=10_000.0)      # everything else is long out of window
    assert len(lim._hits) < before
