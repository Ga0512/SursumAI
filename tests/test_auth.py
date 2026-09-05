"""Password hashing, session tokens and the agent key."""

import os
import stat

import pytest

from central import auth
from core import keys


def test_verify_accepts_the_right_password():
    assert auth.verify_password("correct horse", auth.hash_password("correct horse"))


def test_verify_rejects_the_wrong_password():
    assert not auth.verify_password("wrong", auth.hash_password("correct horse"))


def test_hash_is_salted():
    """Two users with the same password must not share a hash."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_hash_format_records_the_iteration_count():
    scheme, iterations, salt, digest = auth.hash_password("x").split("$")
    assert scheme == "pbkdf2"
    assert int(iterations) == auth.PBKDF2_ITERATIONS
    assert salt and digest


@pytest.mark.parametrize("stored", ["", "garbage", "md5$1$a$b", "pbkdf2$notanint$a$b",
                                    "pbkdf2$1000$only-three"])
def test_verify_never_raises_on_a_corrupt_hash(stored):
    assert not auth.verify_password("x", stored)


def test_tokens_are_unique_and_long():
    tokens = {auth.new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 32 for t in tokens)


def test_token_hash_is_stable_and_not_the_token():
    token = auth.new_token()
    assert auth.hash_token(token) == auth.hash_token(token)
    assert token not in auth.hash_token(token)


def test_deploy_keys_are_internal_and_unique():
    """The per-deploy key locks the model port; it is never shown to anyone,
    and its prefix says so."""
    a, b = auth.new_deploy_key(), auth.new_deploy_key()
    assert a.startswith("sk-internal-") and a != b
    assert not auth.looks_like_api_key(a)


def test_account_api_keys_are_recognisable_and_unique():
    a, b = auth.new_api_key(), auth.new_api_key()
    assert a.startswith("sk-sursum-") and a != b
    assert auth.looks_like_api_key(a)
    assert len(a) > 40


def test_the_display_form_hides_the_middle():
    key = auth.new_api_key()
    shown = auth.key_display(key)
    assert shown.startswith("sk-sursum-") and shown.endswith(key[-4:])
    assert key[14:-4] not in shown


def test_session_expiry_is_in_the_future():
    import time
    assert auth.session_expiry() > time.time()


# ---- agent key ----

def test_key_matches_is_exact():
    assert keys.key_matches("abc", "abc")
    assert not keys.key_matches("abd", "abc")
    assert not keys.key_matches("", "abc")
    assert not keys.key_matches(None, "abc")


def test_dev_key_is_detected():
    assert keys.is_dev_key(keys.DEV_KEY)
    assert not keys.is_dev_key("something-else")


def test_loopback_detection():
    assert keys.is_loopback("127.0.0.1")
    assert keys.is_loopback("localhost")
    assert not keys.is_loopback("0.0.0.0")


def test_key_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_KEY", raising=False)
    monkeypatch.setattr(keys, "KEY_DIR", tmp_path)
    monkeypatch.setattr(keys, "KEY_FILE", tmp_path / "agent.key")
    first = keys.load_or_create_agent_key()
    assert first and not keys.is_dev_key(first)
    assert keys.load_or_create_agent_key() == first


def test_env_agent_key_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "KEY_FILE", tmp_path / "agent.key")
    monkeypatch.setenv("AGENT_KEY", "from-env")
    assert keys.load_or_create_agent_key() == "from-env"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_the_key_file_is_readable_only_by_its_owner(tmp_path, monkeypatch):
    """The agent key is a credential: 0600, created that way from the start."""
    monkeypatch.delenv("AGENT_KEY", raising=False)
    key_file = tmp_path / "agent.key"
    monkeypatch.setattr(keys, "KEY_DIR", tmp_path)
    monkeypatch.setattr(keys, "KEY_FILE", key_file)
    keys.load_or_create_agent_key()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_a_generated_key_has_real_entropy(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_KEY", raising=False)
    monkeypatch.setattr(keys, "KEY_DIR", tmp_path)
    monkeypatch.setattr(keys, "KEY_FILE", tmp_path / "agent.key")
    key = keys.load_or_create_agent_key()
    assert len(key) >= 32
