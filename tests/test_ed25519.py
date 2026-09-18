"""Ed25519 verification, against RFC 8032's own vectors.

A licence check that accepts a forged signature is worse than no check, and a
hand-written implementation is exactly where that happens — so it is tested
against the numbers in the standard, not against itself.
"""
import hashlib

import pytest

from core import ed25519

# RFC 8032, section 7.1: (secret, public, message, signature), all hex.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
        "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("secret,public,message,signature", VECTORS)
def test_the_rfc_vectors_verify(secret, public, message, signature):
    assert ed25519.verify(bytes.fromhex(message), bytes.fromhex(signature),
                          bytes.fromhex(public)) is True


@pytest.mark.parametrize("secret,public,message,signature", VECTORS)
def test_a_changed_message_does_not(secret, public, message, signature):
    tampered = bytes.fromhex(message) + b"!"
    assert ed25519.verify(tampered, bytes.fromhex(signature),
                          bytes.fromhex(public)) is False


@pytest.mark.parametrize("secret,public,message,signature", VECTORS)
def test_another_key_does_not(secret, public, message, signature):
    other = VECTORS[(VECTORS.index((secret, public, message, signature)) + 1) % len(VECTORS)][1]
    assert ed25519.verify(bytes.fromhex(message), bytes.fromhex(signature),
                          bytes.fromhex(other)) is False


def test_a_flipped_bit_in_the_signature_does_not():
    secret, public, message, signature = VECTORS[1]
    sig = bytearray(bytes.fromhex(signature))
    sig[0] ^= 0x01
    assert ed25519.verify(bytes.fromhex(message), bytes(sig), bytes.fromhex(public)) is False


@pytest.mark.parametrize("sig_len,key_len", [(63, 32), (65, 32), (64, 31), (0, 32), (64, 0)])
def test_garbage_lengths_are_refused_not_crashed(sig_len, key_len):
    assert ed25519.verify(b"hi", b"\x00" * sig_len, b"\x00" * key_len) is False


def test_a_signature_whose_s_is_out_of_range_is_refused():
    """s >= L is the classic malleability trap: the same message would have a
    second valid-looking signature."""
    secret, public, message, signature = VECTORS[1]
    sig = bytes.fromhex(signature)
    s = int.from_bytes(sig[32:], "little") + ed25519.L
    assert ed25519.verify(bytes.fromhex(message), sig[:32] + s.to_bytes(32, "little"),
                          bytes.fromhex(public)) is False


def test_the_generator_is_the_one_from_the_standard():
    """Guards the constants: a wrong base point would verify nothing at all —
    or, worse, verify things it should not."""
    assert ed25519.G[0] == 15112221349535400772501151409588531511454012693041857206046113283949847762202
    assert ed25519.G[1] % ed25519.P == 46316835694926478169428394003475163141307993866256225615783033603165251855960


def test_it_agrees_with_a_signature_made_here():
    """Sign with the RFC's own algorithm and check the verifier accepts it —
    the licence issuer (a Cloudflare Worker) signs exactly this way."""
    secret = bytes.fromhex(VECTORS[0][0])
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    public = _compress(ed25519._mul(ed25519.G, a))
    message = b"SursumAI Pro licence"
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % ed25519.L
    big_r = _compress(ed25519._mul(ed25519.G, r))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % ed25519.L
    s = (r + k * a) % ed25519.L
    signature = big_r + s.to_bytes(32, "little")

    assert ed25519.verify(message, signature, public) is True
    assert ed25519.verify(b"another message", signature, public) is False


def _compress(point) -> bytes:
    x, y, z, _ = point
    inv = pow(z, ed25519.P - 2, ed25519.P)
    x, y = x * inv % ed25519.P, y * inv % ed25519.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")
