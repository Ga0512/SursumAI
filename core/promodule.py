"""The Pro module: downloaded by the account that paid, loaded without a restart.

Everyone installs the same SursumAI. What Pro adds — running models on other
machines over SSH — is not in this download: it is a small package that
sursum.ai serves only to an account that paid for it, and that lands in
``~/.sursumai/pro/``.

Two things this file is strict about, because it ends in `import`:

- **The module is verified before it is unpacked.** It is signed with the same
  Ed25519 key as the entitlement, and a byte that does not match the signature
  never reaches the disk. Downloading code and running it without checking who
  signed it would make every SursumAI install one bad DNS answer away from
  running someone else's code.
- **A member of the archive may not escape its directory.** Absolute paths,
  `..` and symlinks are refused — the classic tar traversal, which would let a
  crafted archive write anywhere the user can.

If anything is missing or wrong, this is simply the free edition: no Machines
tab, and nothing else changes.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from core import account, ed25519

log = logging.getLogger("sursumai.pro")

PRO_DIR = Path(os.path.expanduser("~/.sursumai/pro"))
MODULE_NAME = "sursumai_pro"


class ProModuleError(Exception):
    """Something to show the person, in their words."""


def installed_version() -> str | None:
    try:
        return json.loads((PRO_DIR / "module.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


def is_installed() -> bool:
    return (PRO_DIR / MODULE_NAME / "__init__.py").exists()


# ---- getting it ----

def _fetch(token: str, timeout: float = 60) -> tuple[bytes, bytes, str]:
    """Download the module. Returns (archive, signature, version)."""
    req = urllib.request.Request(
        f"{account.API}/module",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "sursumai"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            signature = bytes.fromhex(resp.headers.get("X-Signature", ""))
            version = resp.headers.get("X-Version", "")
            return resp.read(), signature, version
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ProModuleError("this account does not have Pro") from None
        raise ProModuleError(f"the SursumAI account service answered {e.code} — try again in a moment") from None
    except (urllib.error.URLError, OSError):
        raise ProModuleError("could not reach the SursumAI account service — check your connection") from None
    except ValueError:
        raise ProModuleError("the SursumAI account service sent something unexpected") from None


def _safe_members(tar: tarfile.TarFile):
    """Every member, once it is proven to stay inside the directory."""
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise ProModuleError("the Pro module is malformed — it was not installed")
        if member.issym() or member.islnk():
            raise ProModuleError("the Pro module is malformed — it was not installed")
        if not (member.isfile() or member.isdir()):
            raise ProModuleError("the Pro module is malformed — it was not installed")
        yield member


def install(token: str | None = None, timeout: float = 60) -> str:
    """Download and unpack the Pro module. Returns the version."""
    token = token or account.token()
    if not token:
        raise ProModuleError("connect your SursumAI account first")
    if not account.PUBLIC_KEY_HEX:
        raise ProModuleError("this build cannot check signatures — please report this")

    archive, signature, version = _fetch(token, timeout)
    if not ed25519.verify(archive, signature, bytes.fromhex(account.PUBLIC_KEY_HEX)):
        # not "corrupted": a wrong signature means it is not ours
        raise ProModuleError("the Pro module was not signed by SursumAI — "
                             "nothing was installed")

    staging = PRO_DIR.with_name("pro.new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        # members are checked above; `filter` is belt and braces where it exists
        extra = {"filter": "data"} if sys.version_info >= (3, 12) else {}
        tar.extractall(staging, members=_safe_members(tar), **extra)
    (staging / "module.json").write_text(json.dumps({"version": version}) + "\n")

    # swap it in only once it is whole, so a failed download never leaves a
    # half-written module that imports and then explodes
    shutil.rmtree(PRO_DIR, ignore_errors=True)
    staging.rename(PRO_DIR)
    _forget_import()
    log.info("Pro module %s installed", version or "?")
    return version


RESTART_NEEDED = False


def update(timeout: float = 60) -> str | None:
    """Fetch the Pro module again, for an account that has Pro.

    Without this a fix to the module only reached someone who pasted the token
    again. Quiet on failure: the module already on disk keeps working, and our
    server being down must not show up here. Returns the version installed."""
    if not account.is_pro():
        return None
    try:
        return install(timeout=timeout)
    except ProModuleError as e:
        log.debug("the Pro module was not updated: %s", e)
        return None


def _forget_import() -> None:
    """Let the next load() see the code that was just written.

    Python caches a module by name: without this, installing an update would
    keep running the old code until a restart, silently. If the old one is
    already serving routes we cannot unregister them, so that case asks for a
    restart instead of pretending.
    """
    global RESTART_NEEDED
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None and getattr(existing, "_sursumai_loaded", False):
        RESTART_NEEDED = True
        log.info("the new Pro module will be used after SursumAI restarts")
        return
    for name in [n for n in list(sys.modules)
                 if n == MODULE_NAME or n.startswith(MODULE_NAME + ".")]:
        del sys.modules[name]
    # the import system caches what it saw in a directory; the directory was
    # just replaced under it
    importlib.invalidate_caches()


def remove() -> None:
    shutil.rmtree(PRO_DIR, ignore_errors=True)


# ---- loading it ----

def load(app, **context):
    """Hand the running app to the Pro module, if this machine may have it.

    Called at startup and again right after an account connects, so Pro turns
    on without restarting anything. Never raises: a module that fails to load
    leaves the free edition working and says why in the log.
    """
    if not account.is_pro() or not is_installed():
        return False
    if str(PRO_DIR) not in sys.path:
        sys.path.insert(0, str(PRO_DIR))
    try:
        module = __import__(MODULE_NAME)
        if getattr(module, "_sursumai_loaded", False):
            return True
        module.register(app, **context)
        module._sursumai_loaded = True
        log.info("SursumAI Pro %s loaded", installed_version() or "?")
        return True
    except Exception:
        log.exception("the Pro module failed to load — staying on the free edition")
        return False
