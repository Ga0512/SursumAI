"""The SursumAI account: a token here, an entitlement signed there.

The free edition needs no account at all. Buying Pro creates one on
sursum.ai (sign in with GitHub, pay once), and that account hands out a
token — `sursum_pat_…` — which the buyer pastes into their dashboard, the way
GitLab and opencode do it.

With the token, this machine asks once "is this account Pro?" and gets back a
**signed** answer with a date on it. That answer is kept in
``~/.sursumai/account.json`` and re-checked in the background. Two consequences
on purpose:

- **Our server being down never takes Pro away from anyone.** The last answer
  is good for ENTITLEMENT_DAYS; the refresh is quiet and failure is silent
  until the answer actually expires. A paid feature that stops working because
  of our outage is worse than no feature.
- **A refund really removes access**, at the next refresh. That is the reason
  the answer expires at all.

The signature is Ed25519 (`core/ed25519.py`). The private half lives in the
Cloudflare Worker that issues entitlements; nothing that ships to a customer
can produce one.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from core import ed25519

API = os.environ.get("SURSUMAI_API", "https://api.sursum.ai")

# The public half of the key that signs entitlements and the Pro module. The
# private half exists only as a Cloudflare secret and in the signing tool.
PUBLIC_KEY_HEX = os.environ.get(
    "SURSUMAI_ACCOUNT_KEY",
    "a34d25f4fc3ba61969c087706300904c446abf0e6e8e66b281e0685e9559b8e0")

ACCOUNT_FILE = Path(os.path.expanduser("~/.sursumai/account.json"))

REFRESH_AFTER = 24 * 3600
"""Re-ask once a day. Cheap, and it keeps the offline window from drifting."""

TOKEN_PREFIX = "sursum_pat_"


class AccountError(Exception):
    """Something to show the person, in their words."""


class Entitlement:
    def __init__(self, account: str, email: str, plan: str, expires: str):
        self.account = account
        self.email = email
        self.plan = plan
        self.expires = expires          # ISO 8601, UTC

    @property
    def expired(self) -> bool:
        return _epoch(self.expires) <= time.time()

    def to_dict(self) -> dict:
        return {"account": self.account, "email": self.email,
                "plan": self.plan, "expires": self.expires}


def _epoch(iso: str) -> float:
    try:
        return time.mktime(time.strptime(iso.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, TypeError):
        return 0.0


def _b64decode(text: str) -> bytes:
    # validate=True: Python silently drops characters outside the alphabet,
    # which would turn a mangled answer into "not valid" instead of "damaged"
    padded = text.translate(str.maketrans("-_", "+/")) + "=" * (-len(text) % 4)
    return base64.b64decode(padded, validate=True)


def verify(signed: str) -> Entitlement:
    """Check a signed entitlement and return what it says."""
    if not PUBLIC_KEY_HEX:
        raise AccountError("this build cannot check accounts (no signing key) — "
                           "please report this")
    try:
        payload_b64, signature_b64 = signed.split(".")
        payload, signature = _b64decode(payload_b64), _b64decode(signature_b64)
    except Exception:
        raise AccountError("the answer from sursum.ai was damaged") from None
    if not ed25519.verify(payload, signature, bytes.fromhex(PUBLIC_KEY_HEX)):
        raise AccountError("the answer from sursum.ai was not signed by us")
    try:
        data = json.loads(payload.decode())
        return Entitlement(account=data["account"], email=data.get("email", ""),
                           plan=data.get("plan", "free"), expires=data["expires"])
    except Exception:
        raise AccountError("the answer from sursum.ai was damaged") from None


# ---- what is stored on this machine ----

def _read() -> dict:
    try:
        data = json.loads(ACCOUNT_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_FILE.write_text(json.dumps(data, indent=2) + "\n")
    ACCOUNT_FILE.chmod(0o600)          # it holds a token


def token() -> str | None:
    return _read().get("token")


def entitlement() -> Entitlement | None:
    """The last signed answer, if it is still valid. Never raises: a damaged
    file means the free edition, not an app that will not start."""
    signed = _read().get("entitlement")
    if not signed:
        return None
    try:
        ent = verify(signed)
    except AccountError:
        return None
    return None if ent.expired else ent


def is_pro() -> bool:
    ent = entitlement()
    return ent is not None and ent.plan == "pro"


# ---- talking to sursum.ai ----

def _ask(tok: str) -> str:
    """Ask the API for a signed entitlement. Returns the signed string."""
    req = urllib.request.Request(
        f"{API}/entitlement",
        data=b"{}",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                 "User-Agent": "sursumai"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)["entitlement"]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AccountError("this token is not valid — copy it again from "
                               "your account page on sursum.ai") from None
        raise AccountError(f"sursum.ai answered {e.code} — try again in a moment") from None
    except (urllib.error.URLError, OSError):
        raise AccountError("could not reach sursum.ai — check your connection") from None
    except (KeyError, ValueError):
        raise AccountError("sursum.ai sent something unexpected") from None


def connect(tok: str) -> Entitlement:
    """Sign this machine in with a token. Raises AccountError with what to say."""
    tok = (tok or "").strip()
    if not tok:
        raise AccountError("paste the token from your account page")
    if not tok.startswith(TOKEN_PREFIX):
        raise AccountError(f"that does not look like a SursumAI token — "
                           f"it starts with {TOKEN_PREFIX}")
    signed = _ask(tok)
    ent = verify(signed)            # never trust an answer we did not sign
    _write({"token": tok, "entitlement": signed,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return ent


def refresh(force: bool = False) -> Entitlement | None:
    """Re-ask in the background. Silent on failure — the stored answer stands
    until it expires, so an outage on our side is invisible here."""
    data = _read()
    tok = data.get("token")
    if not tok:
        return None
    if not force and time.time() - _epoch(data.get("checked_at", "")) < REFRESH_AFTER:
        return entitlement()
    try:
        signed = _ask(tok)
        ent = verify(signed)
    except AccountError:
        return entitlement()
    _write({**data, "entitlement": signed,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return ent


def disconnect() -> None:
    """Sign this machine out — how you move Pro to another computer."""
    ACCOUNT_FILE.unlink(missing_ok=True)


def status() -> dict:
    """What the dashboard shows."""
    ent = entitlement()
    # where "Go Pro" sends people: the same service this machine asks, so a
    # single setting (API) points both at it
    if ent is None:
        return {"pro": False, "connected": token() is not None, "store": API}
    return {"pro": ent.plan == "pro", "connected": True, "store": API, **ent.to_dict()}
