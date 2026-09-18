"""Ed25519 signature verification, in pure Python.

Only `verify`: SursumAI never signs anything — the private key lives in the
Cloudflare Worker that issues licences, and this side only checks them.

It is here instead of `cryptography` because `requirements.txt` is deliberately
four packages, and a licence check must not be the reason an install fails on
a machine where a wheel will not build. This is the reference implementation
from RFC 8032, which the tests exercise against that RFC's own vectors.

Speed does not matter: one verification per process start.
"""
from __future__ import annotations

import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)          # noqa: E741 — the RFC calls it I


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = x * I % P
    if (x * x - xx) % P != 0:
        return None
    if x % 2 != sign:
        x = P - x
    return x


# Points are (X, Y, Z, T) in extended coordinates: addition with no branches
# and no inversions, which keeps this short and side-channel-irrelevant (there
# is no secret here to leak).
G_Y = 4 * pow(5, P - 2, P) % P
G_X = _recover_x(G_Y, 0)
G = (G_X, G_Y, 1, G_X * G_Y % P)
ZERO = (0, 1, 1, 0)


def _add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    dd = 2 * z1 * z2 % P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(p, n: int):
    q = ZERO
    while n > 0:
        if n & 1:
            q = _add(q, p)
        p = _add(p, p)
        n >>= 1
    return q


def _equal(p, q) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Is `signature` a valid Ed25519 signature of `message` by `public_key`?"""
    if len(signature) != 64 or len(public_key) != 32:
        return False
    point = _decompress(public_key)
    if point is None:
        return False
    r = _decompress(signature[:32])
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = int.from_bytes(
        hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % L
    return _equal(_mul(G, s), _add(r, _mul(point, h)))
