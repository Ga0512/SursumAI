"""A ceiling per credential on /v1, so a leaked key is not a free GPU.

The account API key was the only thing in front of the models, and nothing
counted how often it was used. On loopback that is fine — the only caller is
the person at the keyboard. `SURSUMAI_BIND` puts the port on the network, and
from that moment a key that escaped into somebody's script, a git history or a
chat log is an open invitation to someone else's inference.

So the default follows the exposure instead of being a number we picked for
everyone: **off on loopback, on when bound to anything else.** Turning it on
for every install would have broken the workload this project documents as
normal — four documents in parallel, a bench, a batch run — for a risk those
installs do not have.

In memory on purpose. The central is one process, so a dict is the whole
truth; a row per request would mean a database write per request, which is a
worse problem than the one being solved. The cost is that the window resets on
restart, which is the right trade for a limit whose job is to stop sustained
abuse, not to bill anyone.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

WINDOW = 60.0           # seconds; the limit is "per minute"
LIMIT_ENV = "SURSUMAI_RATE_LIMIT"
EXPOSED_DEFAULT = 120   # generous for one person, a wall for a scraper


def configured_limit() -> int:
    """Requests per minute per credential. 0 disables the limit.

    An explicit `SURSUMAI_RATE_LIMIT` always wins, including `0` to turn the
    limit off on an exposed install. With nothing set, the limit exists only
    when the operator has asked for the network.
    """
    raw = os.environ.get(LIMIT_ENV, "").strip()
    if raw:
        try:
            return max(int(raw), 0)
        except ValueError:
            # a typo in an env var must not be why a service refuses to boot,
            # and must not silently mean "no limit" on an exposed port either
            return EXPOSED_DEFAULT
    bind = os.environ.get("SURSUMAI_BIND", "").strip()
    if bind and bind not in ("127.0.0.1", "localhost", "::1"):
        return EXPOSED_DEFAULT
    return 0


class Limiter:
    """A sliding window of request times per identity.

    Sliding, not fixed buckets: a fixed minute lets twice the limit through
    across a boundary, which on a GPU is the difference between a queue that
    drains and one that does not.
    """

    def __init__(self, limit: int | None = None, window: float = WINDOW) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        # read late, so a test (or a restart with a new environment) is not
        # fighting a value frozen at import
        return configured_limit() if self._limit is None else self._limit

    def check(self, identity: str, now: float | None = None) -> float:
        """Record a request. Returns 0.0 if allowed, else seconds to wait.

        `identity` must never be a plaintext credential: callers pass the
        same one-way digest the database stores, so a memory dump or a log of
        this structure cannot be replayed as a key.
        """
        limit = self.limit
        if limit <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        cutoff = now - self._window
        with self._lock:
            hits = self._hits.get(identity)
            if hits is None:
                hits = self._hits[identity] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                # how long until the oldest request leaves the window
                return max(hits[0] - cutoff, 0.001)
            hits.append(now)
            # an identity that stopped calling must not hold memory forever:
            # a key is cheap to create and this dict is unbounded otherwise
            if len(self._hits) > 4096:
                self._evict(cutoff)
            return 0.0

    def _evict(self, cutoff: float) -> None:
        """Drop identities with nothing left in the window. Caller holds the lock."""
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
