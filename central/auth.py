from __future__ import annotations

import hashlib
import hmac
import secrets
import time

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 30 * 24 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    )
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Sessions are stored hashed: a stolen database must not hand out
    working bearer tokens. Tokens are already 256 bits of entropy, so a
    single SHA-256 (no salt/stretching) is enough and keeps lookups indexable."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_deploy_key() -> str:
    """Bearer key for a single deployment's own OpenAI-compatible endpoint."""
    return "sk-sursum-" + secrets.token_urlsafe(24)


def session_expiry() -> float:
    return time.time() + SESSION_TTL_SECONDS
