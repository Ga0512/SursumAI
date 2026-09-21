"""The SursumAI account.

The token is pasted on this machine; the answer about what it is entitled to
is signed by sursum.ai and kept here with a date on it. The two things this
must get right, in both directions: our outage never takes Pro away from
someone who paid, and a refund does take it away.
"""
import base64
import hashlib
import json
import time
import urllib.error

import pytest

from core import account, ed25519

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def _public(secret: bytes = SECRET) -> bytes:
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    x, y, z, _ = ed25519._mul(ed25519.G, a)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    x, y = x * inv % ed25519.P, y * inv % ed25519.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _sign(message: bytes, secret: bytes = SECRET) -> bytes:
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % ed25519.L
    x, y, z, _ = ed25519._mul(ed25519.G, r)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    big_r = ((y * inv % ed25519.P) | ((x * inv % ed25519.P & 1) << 255)).to_bytes(32, "little")
    k = int.from_bytes(hashlib.sha512(big_r + _public(secret) + message).digest(),
                       "little") % ed25519.L
    return big_r + ((r + k * a) % ed25519.L).to_bytes(32, "little")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _iso(offset_days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset_days * 86400))


def _signed(plan="pro", email="buyer@example.com", days=30, secret=SECRET) -> str:
    payload = json.dumps({"account": "acc_1", "email": email, "plan": plan,
                          "expires": _iso(days)}).encode()
    return f"{_b64(payload)}.{_b64(_sign(payload, secret))}"


TOKEN = "sursum_pat_abc123"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(account, "PUBLIC_KEY_HEX", _public().hex())
    monkeypatch.setattr(account, "ACCOUNT_FILE", tmp_path / ".sursumai" / "account.json")


@pytest.fixture
def api(monkeypatch):
    """The entitlement endpoint, faked. `answer` is what it replies with."""
    state = {"answer": _signed(), "calls": 0, "raise": None}

    def _ask(tok):
        state["calls"] += 1
        state["token"] = tok
        if state["raise"] is not None:
            raise state["raise"]
        return state["answer"]

    monkeypatch.setattr(account, "_ask", _ask)
    return state


# ---- connecting ----

def test_pasting_the_token_turns_pro_on(api):
    ent = account.connect(TOKEN)

    assert ent.plan == "pro" and ent.email == "buyer@example.com"
    assert account.is_pro() is True
    assert api["token"] == TOKEN
    assert account.status()["email"] == "buyer@example.com"


def test_without_an_account_this_is_free():
    assert account.is_pro() is False
    status = account.status()
    assert status["pro"] is False and status["connected"] is False
    assert status["store"] == account.API          # where Go Pro sends people


@pytest.mark.parametrize("typed,message", [
    ("", "paste the token"),
    ("hunter2", "does not look like"),
    ("  ", "paste the token"),
])
def test_what_was_typed_wrong_is_said_in_their_words(typed, message, api):
    with pytest.raises(account.AccountError, match=message):
        account.connect(typed)
    assert api["calls"] == 0          # not even asked


def test_an_answer_signed_by_someone_else_is_refused(api):
    other = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    api["answer"] = _signed(secret=other)

    with pytest.raises(account.AccountError, match="not signed by us"):
        account.connect(TOKEN)
    assert account.is_pro() is False


def test_editing_the_plan_in_a_real_answer_breaks_it(api):
    payload, signature = _signed(plan="free").split(".")
    tampered = json.loads(base64.urlsafe_b64decode(payload + "==").decode())
    tampered["plan"] = "pro"
    api["answer"] = f"{_b64(json.dumps(tampered).encode())}.{signature}"

    with pytest.raises(account.AccountError, match="not signed by us"):
        account.connect(TOKEN)


def test_the_file_with_the_token_is_not_readable_by_others(api):
    account.connect(TOKEN)
    assert oct(account.ACCOUNT_FILE.stat().st_mode)[-3:] == "600"


def test_a_free_account_connects_but_is_not_pro(api):
    api["answer"] = _signed(plan="free")
    account.connect(TOKEN)

    assert account.is_pro() is False
    assert account.status()["connected"] is True     # signed in, just not paid


# ---- staying Pro when we are down ----

def test_our_outage_does_not_take_pro_away(api):
    account.connect(TOKEN)
    api["raise"] = account.AccountError("could not reach sursum.ai")

    assert account.refresh(force=True) is not None
    assert account.is_pro() is True                  # the stored answer stands


def test_an_answer_that_ran_out_stops_being_pro(api):
    """This is why the answer has a date: a refund reaches the machine at the
    next refresh, and a machine that never refreshes stops on its own."""
    account._write({"token": TOKEN, "entitlement": _signed(days=-1), "checked_at": _iso(-2)})

    assert account.entitlement() is None
    assert account.is_pro() is False


def test_a_refund_removes_pro_at_the_next_refresh(api):
    account.connect(TOKEN)
    assert account.is_pro() is True

    api["answer"] = _signed(plan="free")             # refunded on our side
    account.refresh(force=True)

    assert account.is_pro() is False


def test_refreshing_is_not_done_on_every_call(api):
    account.connect(TOKEN)
    before = api["calls"]

    account.refresh()
    account.refresh()

    assert api["calls"] == before                    # asked again only after a day


def test_a_damaged_file_means_free_not_a_crash():
    account.ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    account.ACCOUNT_FILE.write_text("{not json")

    assert account.entitlement() is None
    assert account.is_pro() is False
    assert account.refresh() is None


def test_disconnecting_gives_the_machine_back_to_free(api):
    account.connect(TOKEN)
    account.disconnect()

    assert account.is_pro() is False
    assert account.token() is None
    account.disconnect()                             # twice is not an error


def test_a_build_with_no_signing_key_accepts_nothing(api, monkeypatch):
    monkeypatch.setattr(account, "PUBLIC_KEY_HEX", "")
    with pytest.raises(account.AccountError, match="cannot check accounts"):
        account.connect(TOKEN)


# ---- what the API answers ----

def test_a_rejected_token_says_to_copy_it_again(monkeypatch):
    def _401(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(account.urllib.request, "urlopen", _401)
    with pytest.raises(account.AccountError, match="copy it again"):
        account.connect(TOKEN)


def test_no_connection_says_so(monkeypatch):
    def _down(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(account.urllib.request, "urlopen", _down)
    with pytest.raises(account.AccountError, match="could not reach"):
        account.connect(TOKEN)


def test_the_token_goes_in_the_authorization_header(monkeypatch):
    seen = {}

    class _Resp:
        def read(self):
            return json.dumps({"entitlement": _signed()}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(account.urllib.request, "urlopen", _urlopen)
    account.connect(TOKEN)

    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["url"].endswith("/entitlement")
