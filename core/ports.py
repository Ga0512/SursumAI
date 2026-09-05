"""Deploy ports.

Deploys listen on 9000-9099. The central allocates the port (it is the only
process that can see every deploy) and writes it into the spec; the agent just
uses what it is told.

Older deploys were created before ports were allocated and fall back to a hash
of the deploy id — which is exactly the bug this module exists to end: a hash
into 100 slots collides about half the time by the twelfth deploy, and nothing
noticed, so the second deploy quietly landed on a port the first one already
held. `legacy_port` stays only so those existing deploys keep answering on the
port they are actually listening on.
"""

from __future__ import annotations

import hashlib
import socket
import time

PORT_MIN = 9000
PORT_MAX = 9099
PORT_RANGE = range(PORT_MIN, PORT_MAX + 1)


class NoPortAvailable(Exception):
    """All 100 deploy ports are taken."""


def legacy_port(deploy_id: str) -> int:
    """The port a pre-allocation deploy is listening on. Do not use for new ones."""
    digest = int(hashlib.sha256(deploy_id.encode()).hexdigest()[:8], 16)
    return PORT_MIN + (digest % 100)


def in_range(port: int | None) -> bool:
    return port is not None and PORT_MIN <= port <= PORT_MAX


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can a server bind this port right now?

    Checked without SO_REUSEADDR on purpose: we want "is anything listening
    here", not "could I steal it".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def wait_until_free(port: int, timeout: float = 8.0, host: str = "127.0.0.1") -> bool:
    """Wait briefly for a port to come free.

    Tearing a container down and releasing its port is not instant, so a
    deploy created right after destroying another one would otherwise be
    rejected for a port that is about to be free. Only worth a few seconds:
    anything longer really is someone else's port.
    """
    deadline = time.monotonic() + timeout
    while True:
        if is_free(port, host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def first_free(taken: set[int]) -> int:
    """Lowest port in the range that no deploy holds.

    Lowest-first (not random) keeps the range compact and the assignment
    reproducible, which makes a busy machine easy to read.
    """
    for port in PORT_RANGE:
        if port not in taken:
            return port
    raise NoPortAvailable(
        f"all {len(PORT_RANGE)} deploy ports ({PORT_MIN}-{PORT_MAX}) are in use — "
        "destroy a deployment before creating another one"
    )
