"""Secrets shared by the central and the agent.

The agent key is generated once, on the first run, and stored in
``~/.sursumai/agent.key`` (0600). Both processes read the same file, so the
user never has to set an environment variable — and the well-known
``dev-agent-key`` never ends up protecting a machine that is reachable from
the network.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

DEV_KEY = "dev-agent-key"

KEY_DIR = Path(os.environ.get("SURSUMAI_HOME", Path.home() / ".sursumai"))
KEY_FILE = KEY_DIR / "agent.key"


def _write_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # create with 0600 from the start: never widen, never leak in between
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (key + "\n").encode())
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_or_create_agent_key() -> str:
    """The shared agent key. AGENT_KEY in the environment always wins;
    otherwise read (or create) ~/.sursumai/agent.key."""
    env = os.environ.get("AGENT_KEY")
    if env:
        return env
    try:
        existing = KEY_FILE.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    try:
        _write_key(KEY_FILE, key)
    except OSError as e:
        raise RuntimeError(
            f"could not create the agent key at {KEY_FILE}: {e}. "
            "Set AGENT_KEY in the environment instead."
        ) from None
    return key


def is_dev_key(key: str) -> bool:
    return hmac.compare_digest(key, DEV_KEY)


def key_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison — never leak the key through timing."""
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")
