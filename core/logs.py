"""Logging the operator can turn up, and deploy logs that stop growing.

Two things a long-lived install needs and the app did not have:

* a level. Nothing in the project ever called `logging.basicConfig`, so every
  `log.debug` in the codebase was unreachable — there was no way to ask a
  machine what it was doing. `SURSUMAI_LOG=debug` is that way.
* a ceiling on the deploy logs. `metrics` is pruned to `METRICS_KEEP` rows and
  has an index; `sursumai-logs/<id>.log` had nothing, and a chatty runtime
  writing for weeks fills the disk of the machine it is serving from.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

LEVEL_ENV = "SURSUMAI_LOG"

# 64 MiB holds days of a normal start plus a download, and still fits in a
# paste. Keeping the newest half means a trim never leaves a near-full file
# that trims again on the next poll.
MAX_BYTES = max(int(os.environ.get("SURSUMAI_LOG_MAX_MB", "64")), 1) * 1024 * 1024
KEEP_BYTES = MAX_BYTES // 2

_TRIM_MARK = b"[sursumai] ---- earlier output dropped to keep this log bounded ----\n"


def setup(service: str) -> None:
    """Configure the root logger from `SURSUMAI_LOG` (default `info`).

    Idempotent: `uvicorn --reload` imports the app twice, and two handlers on
    the root logger print every line twice.
    """
    name = os.environ.get(LEVEL_ENV, "info").strip().lower()
    level = getattr(logging, name.upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    root = logging.getLogger()
    if not any(getattr(h, "_sursumai", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s %(levelname)s {service} %(name)s: %(message)s"))
        handler._sursumai = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    # our own loggers follow the root; uvicorn keeps its own access log
    logging.getLogger("sursumai").setLevel(level)


def trim(path: Path, max_bytes: int | None = None,
         keep_bytes: int | None = None) -> int:
    """Cap a deploy log in place, keeping the newest `keep_bytes`.

    The ceiling is read from the module here rather than bound as a default
    argument: a default is evaluated at import, so `MAX_BYTES` would be frozen
    at whatever the environment said then and could never be changed — not by
    a test, and not by anything that configures this after import. `keep_bytes`
    is derived from the ceiling actually in force, so passing a smaller
    `max_bytes` alone cannot ask to keep more than the cap allows.

    Rewrite in place rather than rename: the runtime writing this file holds
    the fd open in append mode (`open(path, "ab")`), and append always writes
    at the end of the *inode*, so a rename would leave the process writing to
    a file nobody reads while the new one stays empty. Truncating the same
    inode is what `logrotate`'s `copytruncate` does, with the same caveat: a
    line written during the rewrite can be lost. Losing a line of a log that
    is already being discarded is the cheapest of the options here.

    Returns the size of the file afterwards. A log that is already small is
    not touched, so this is safe to call on every status poll.
    """
    if max_bytes is None:
        max_bytes = MAX_BYTES
    if keep_bytes is None:
        keep_bytes = max_bytes // 2
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= max_bytes:
        return size
    try:
        with open(path, "r+b") as f:
            f.seek(size - keep_bytes)
            tail = f.read()
            # never start the kept text mid-line: a half line looks like
            # corruption to whoever opens the log next
            newline = tail.find(b"\n")
            if 0 <= newline < len(tail) - 1:
                tail = tail[newline + 1:]
            f.seek(0)
            f.write(_TRIM_MARK)
            f.write(tail)
            f.truncate()
            return f.tell()
    except OSError:
        # a log we cannot rewrite is not a reason to fail a status poll
        logging.getLogger("sursumai.logs").debug("could not trim %s", path)
        return size
