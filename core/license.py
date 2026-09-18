"""The Pro licence: one signed string, checked on this machine.

A licence is issued by the Cloudflare Worker after a payment goes through, and
looks like

    SURSUM-<payload>.<signature>

where the payload is the buyer's e-mail and order, and the signature is
Ed25519 over that payload. SursumAI only ever *verifies*: the private key
lives in the Worker, never in anything a customer downloads.

Deliberately offline. A licence check that phones home turns someone else's
outage — or a laptop on a plane — into "your paid features are gone", and the
whole point of this product is that it runs on your own machine.

None of this stops a determined person from editing this file; nothing running
on someone's own computer can. It marks the line between using SursumAI Pro
and not paying for it, which is what a licence is for.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from core import ed25519

# The public half of the key the Worker signs with. Empty in a development
# tree; `release.sh` refuses to publish without it.
PUBLIC_KEY_HEX = os.environ.get("SURSUMAI_LICENSE_KEY", "")

LICENSE_FILE = Path(os.path.expanduser("~/.sursumai/license.json"))

GRACE_DAYS = 0
"""Licences do not expire. The field exists in the payload so that a future
subscription could, without changing the format."""


class LicenseError(Exception):
    """Something to show the buyer, in their words."""


class License:
    def __init__(self, email: str, order: str, issued: str, plan: str = "pro"):
        self.email = email
        self.order = order
        self.issued = issued
        self.plan = plan

    def to_dict(self) -> dict:
        return {"email": self.email, "order": self.order,
                "issued": self.issued, "plan": self.plan}


def _b64decode(text: str) -> bytes:
    # validate=True on purpose: by default Python drops characters outside the
    # alphabet, so a mangled key would decode to something and come back as
    # "not valid" — sending the buyer to ask for a refund instead of to copy
    # the key again.
    # (urlsafe_b64decode itself takes no validate flag, hence the translation)
    padded = text.translate(str.maketrans("-_", "+/")) + "=" * (-len(text) % 4)
    return base64.b64decode(padded, validate=True)


def parse(key: str) -> License:
    """Check a licence key and return what it says. Raises LicenseError."""
    key = (key or "").strip().replace("\n", "")
    if not key:
        raise LicenseError("paste your licence key")
    if not key.startswith("SURSUM-"):
        raise LicenseError("that does not look like a SursumAI licence key — "
                           "it starts with SURSUM-")
    body = key[len("SURSUM-"):]
    if body.count(".") != 1:
        raise LicenseError("this licence key is incomplete — copy the whole line")
    payload_b64, signature_b64 = body.split(".")
    try:
        payload = _b64decode(payload_b64)
        signature = _b64decode(signature_b64)
    except Exception:
        raise LicenseError("this licence key is damaged — copy it again") from None

    if not PUBLIC_KEY_HEX:
        raise LicenseError("this build cannot check licences (no signing key) — "
                           "please report this")
    if not ed25519.verify(payload, signature, bytes.fromhex(PUBLIC_KEY_HEX)):
        raise LicenseError("this licence key is not valid. If you typed it, "
                           "paste it instead — one wrong character is enough")

    try:
        data = json.loads(payload.decode())
        return License(email=data["email"], order=data["order"],
                       issued=data.get("issued", ""), plan=data.get("plan", "pro"))
    except Exception:
        raise LicenseError("this licence key is damaged — copy it again") from None


def load() -> License | None:
    """The licence on this machine, or None. Never raises: a broken file must
    not stop SursumAI from starting, it only means the free edition."""
    try:
        data = json.loads(LICENSE_FILE.read_text())
        return parse(data["key"])
    except (OSError, ValueError, KeyError, LicenseError):
        return None


def activate(key: str) -> License:
    """Verify a key and keep it. Raises LicenseError with what to tell the buyer."""
    license = parse(key)
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps(
        {"key": key.strip(), "activated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        indent=2) + "\n")
    LICENSE_FILE.chmod(0o600)
    return license


def deactivate() -> None:
    """Forget the licence on this machine — for moving it to another one."""
    LICENSE_FILE.unlink(missing_ok=True)


def is_pro() -> bool:
    return load() is not None


def status() -> dict:
    """What the dashboard shows: free, or Pro and who it belongs to."""
    license = load()
    if license is None:
        return {"pro": False}
    return {"pro": True, **license.to_dict()}
