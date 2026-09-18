"""The Pro licence.

Signed by the Worker that takes the money, checked here with the public half —
so a key cannot be typed up by hand, and checking one needs no network.
"""
import base64
import hashlib
import json

import pytest

from core import ed25519, license as lic

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def _keypair(secret: bytes = SECRET) -> tuple[bytes, bytes]:
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    x, y, z, _ = ed25519._mul(ed25519.G, a)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    x, y = x * inv % ed25519.P, y * inv % ed25519.P
    return secret, (y | ((x & 1) << 255)).to_bytes(32, "little")


def _sign(message: bytes, secret: bytes = SECRET) -> bytes:
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    _, public = _keypair(secret)
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % ed25519.L
    x, y, z, _ = ed25519._mul(ed25519.G, r)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    big_r = ((y * inv % ed25519.P) | ((x * inv % ed25519.P & 1) << 255)).to_bytes(32, "little")
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % ed25519.L
    return big_r + ((r + k * a) % ed25519.L).to_bytes(32, "little")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _key(email="buyer@example.com", order="cs_test_1", secret=SECRET) -> str:
    payload = json.dumps({"email": email, "order": order,
                          "issued": "2026-09-18", "plan": "pro"}).encode()
    return f"SURSUM-{_b64(payload)}.{_b64(_sign(payload, secret))}"


@pytest.fixture(autouse=True)
def signing_key(tmp_path, monkeypatch):
    _, public = _keypair()
    monkeypatch.setattr(lic, "PUBLIC_KEY_HEX", public.hex())
    monkeypatch.setattr(lic, "LICENSE_FILE", tmp_path / ".sursumai" / "license.json")


def test_a_real_key_activates_and_says_who_bought_it():
    license = lic.activate(_key(email="gabriel@example.com"))

    assert license.email == "gabriel@example.com"
    assert lic.is_pro() is True
    assert lic.status() == {"pro": True, "email": "gabriel@example.com",
                            "order": "cs_test_1", "issued": "2026-09-18", "plan": "pro"}


def test_without_a_licence_this_is_the_free_edition():
    assert lic.is_pro() is False
    assert lic.status() == {"pro": False}


def test_a_key_signed_by_anyone_else_is_refused():
    """The whole point: keys cannot be made up, only bought."""
    other = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    with pytest.raises(lic.LicenseError, match="not valid"):
        lic.parse(_key(secret=other))
    assert lic.is_pro() is False


def test_editing_the_e_mail_in_a_real_key_breaks_it():
    key = _key(email="buyer@example.com")
    payload, signature = key[len("SURSUM-"):].split(".")
    tampered = json.loads(base64.urlsafe_b64decode(payload + "==").decode())
    tampered["email"] = "someone.else@example.com"
    forged = f"SURSUM-{_b64(json.dumps(tampered).encode())}.{signature}"

    with pytest.raises(lic.LicenseError, match="not valid"):
        lic.parse(forged)


@pytest.mark.parametrize("typed,message", [
    ("", "paste your licence key"),
    ("hunter2", "does not look like"),
    ("SURSUM-abc", "incomplete"),
    ("SURSUM-!!!.!!!", "damaged"),
])
def test_what_the_buyer_typed_wrong_is_said_in_their_words(typed, message):
    with pytest.raises(lic.LicenseError, match=message):
        lic.parse(typed)


def test_whitespace_from_copying_is_forgiven():
    lic.parse(f"  {_key()}\n")


def test_the_licence_file_is_not_readable_by_others():
    lic.activate(_key())
    assert oct(lic.LICENSE_FILE.stat().st_mode)[-3:] == "600"


def test_a_damaged_licence_file_means_free_not_a_crash():
    """SursumAI must start even if this file is garbage."""
    lic.LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lic.LICENSE_FILE.write_text("{not json")
    assert lic.load() is None
    assert lic.is_pro() is False


def test_deactivating_gives_the_machine_back_to_free():
    lic.activate(_key())
    lic.deactivate()
    assert lic.is_pro() is False
    lic.deactivate()          # twice is not an error: it is how you retry


def test_a_build_with_no_signing_key_says_so_instead_of_accepting_anything(monkeypatch):
    monkeypatch.setattr(lic, "PUBLIC_KEY_HEX", "")
    with pytest.raises(lic.LicenseError, match="cannot check licences"):
        lic.parse(_key())


# ---- through the API ----

@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from central import app as central_app
    from central.db import Store

    store = Store(path=tmp_path / "api.db")
    monkeypatch.setattr(central_app, "store", store)
    monkeypatch.setattr(central_app, "_reconcile_stale", lambda *a, **kw: None)
    with fastapi_testclient.TestClient(central_app.app) as c:
        yield c


def _token(client):
    resp = client.post("/auth/register", json={"email": "a@b.com", "password": "hunter2hunter2"})
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_the_dashboard_sees_free_then_pro(client):
    h = _token(client)
    assert client.get("/meta/license", headers=h).json() == {"pro": False}

    body = client.post("/meta/license", json={"key": _key(email="buyer@example.com")},
                       headers=h).json()
    assert body["pro"] is True and body["email"] == "buyer@example.com"
    assert client.get("/meta/license", headers=h).json()["pro"] is True

    client.delete("/meta/license", headers=h)
    assert client.get("/meta/license", headers=h).json() == {"pro": False}


def test_a_bad_key_comes_back_as_something_to_read(client):
    h = _token(client)
    resp = client.post("/meta/license", json={"key": "SURSUM-nope"}, headers=h)
    assert resp.status_code == 422
    assert "incomplete" in resp.json()["detail"]


def test_a_licence_needs_a_login(client):
    assert client.get("/meta/license").status_code == 401
    assert client.post("/meta/license", json={"key": _key()}).status_code == 401


def test_an_api_key_cannot_touch_the_licence(client):
    h = _token(client)
    key = client.post("/api-keys", json={"name": "script"}, headers=h).json()["key"]
    api = {"Authorization": f"Bearer {key}"}
    assert client.get("/meta/license", headers=api).status_code == 403
    assert client.delete("/meta/license", headers=api).status_code == 403
