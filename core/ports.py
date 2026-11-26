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
import os
import socket
import time

PORT_MIN = 9000
PORT_MAX = 9099
PORT_RANGE = range(PORT_MIN, PORT_MAX + 1)


# --- the three services -------------------------------------------------------
# Defaults for someone double-clicking the icon; environment for someone running
# SursumAI as a service, where the port is the host's decision, not ours. One
# place reads them, so the CLI, start.sh, the web proxy and the URL the user
# copies can never disagree.

def _service_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"{name}={port} is outside 1-65535")
    if PORT_MIN <= port <= PORT_MAX:
        raise SystemExit(f"{name}={port} is inside {PORT_MIN}-{PORT_MAX}, "
                         "which is where deployed models listen")
    return port


WEB_PORT = _service_port("SURSUMAI_WEB_PORT", 3000)
CENTRAL_PORT = _service_port("SURSUMAI_CENTRAL_PORT", 8001)
AGENT_PORT = _service_port("SURSUMAI_AGENT_PORT", 8010)

if len({WEB_PORT, CENTRAL_PORT, AGENT_PORT}) != 3:
    raise SystemExit("the web, central and agent ports must be three different ports")


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


def cors_origins() -> list[str]:
    """Browser origins allowed to call the central directly.

    The dashboard never needs this: every call it makes goes to `/api` on its
    own origin, through the web proxy, and `publicBaseUrl()` is only ever
    shown as text. So this list exists for the other case — the client's own
    front-end calling `/v1` from a browser — and the hardcoded
    `localhost:3000` pair served nobody once `SURSUMAI_BIND` moved the
    dashboard to a LAN address.

    `SURSUMAI_CORS_ORIGINS` is a comma-separated list, or `*` for any origin.
    `*` is not the hole it looks like here: this API authenticates with a
    bearer token, never a cookie, so a hostile page cannot make an
    authenticated request — it has no way to read the key out of another
    origin's localStorage. It would be a hole if auth were cookie-based.
    """
    raw = os.environ.get("SURSUMAI_CORS_ORIGINS", "").strip()
    if raw == "*":
        return ["*"]
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    origins = [f"http://localhost:{WEB_PORT}", f"http://127.0.0.1:{WEB_PORT}"]
    host = os.environ.get("SURSUMAI_BIND", "").strip()
    # the bound address is where the dashboard is actually reachable; 0.0.0.0
    # means every interface, which is not an origin a browser can ever send,
    # so there is nothing to add for it
    if host and host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        origins.append(f"http://{host}:{WEB_PORT}")
    return origins


def bind_host() -> str:
    """Interface a model server should listen on.

    Same rule as the three SursumAI processes: loopback unless the operator
    asks for the network with SURSUMAI_BIND. Model ports used to be published
    on 0.0.0.0 regardless, which put the model on the public internet of any
    cloud VM — the deploy's key was the only thing in front of it. It also
    failed outright on hosts that refuse a 0.0.0.0 bind.
    """
    return os.environ.get("SURSUMAI_BIND", "127.0.0.1")
