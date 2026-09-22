"""The Pro module: downloaded, verified, unpacked, loaded.

This file ends in `import`, so the tests are mostly about refusing things: an
archive nobody signed, an archive signed by somebody else, and an archive that
tries to write outside its own directory.
"""
import hashlib
import io
import json
import sys
import tarfile

import pytest

from core import account, ed25519, promodule

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
OTHER = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")


def _public(secret=SECRET) -> bytes:
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    x, y, z, _ = ed25519._mul(ed25519.G, a)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    return ((y * inv % ed25519.P) | ((x * inv % ed25519.P & 1) << 255)).to_bytes(32, "little")


def _sign(message: bytes, secret=SECRET) -> bytes:
    h = hashlib.sha512(secret).digest()
    a = (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % ed25519.L
    x, y, z, _ = ed25519._mul(ed25519.G, r)
    inv = pow(z, ed25519.P - 2, ed25519.P)
    big_r = ((y * inv % ed25519.P) | ((x * inv % ed25519.P & 1) << 255)).to_bytes(32, "little")
    k = int.from_bytes(hashlib.sha512(big_r + _public(secret) + message).digest(),
                       "little") % ed25519.L
    return big_r + ((r + k * a) % ed25519.L).to_bytes(32, "little")


def _archive(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


MODULE = {"sursumai_pro/__init__.py": (
    "REGISTERED = []\n"
    "def register(app, **context):\n"
    "    REGISTERED.append((app, context))\n"
)}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A fresh machine for every test.

    Including Python's own module cache and sys.path: the module is imported
    for real here, and one test's copy would otherwise answer for the next.
    """
    monkeypatch.setattr(promodule, "PRO_DIR", tmp_path / "pro")
    monkeypatch.setattr(promodule, "RESTART_NEEDED", False)
    monkeypatch.setattr(account, "PUBLIC_KEY_HEX", _public().hex())
    monkeypatch.setattr(account, "ACCOUNT_FILE", tmp_path / "account.json")
    path_before = list(sys.path)
    _drop_imported()
    yield
    _drop_imported()
    sys.path[:] = path_before


def _drop_imported():
    for name in [n for n in list(sys.modules)
                 if n == promodule.MODULE_NAME or n.startswith(promodule.MODULE_NAME + ".")]:
        del sys.modules[name]


@pytest.fixture
def served(monkeypatch):
    """What sursum.ai serves at /module."""
    state = {"archive": _archive(MODULE), "secret": SECRET, "version": "1.0.0"}

    def _fetch(token, timeout=60):
        state["token"] = token
        return state["archive"], _sign(state["archive"], state["secret"]), state["version"]

    monkeypatch.setattr(promodule, "_fetch", _fetch)
    return state


def _pro_account(monkeypatch, pro=True):
    monkeypatch.setattr(account, "token", lambda: "sursum_pat_x")
    monkeypatch.setattr(account, "is_pro", lambda: pro)


# ---- installing ----

def test_a_signed_module_is_installed(served, monkeypatch):
    _pro_account(monkeypatch)
    version = promodule.install()

    assert version == "1.0.0"
    assert promodule.is_installed() is True
    assert promodule.installed_version() == "1.0.0"
    assert served["token"] == "sursum_pat_x"


def test_a_module_signed_by_someone_else_never_touches_the_disk(served, monkeypatch):
    """Otherwise a hijacked download runs as the user, on every install."""
    _pro_account(monkeypatch)
    served["secret"] = OTHER

    with pytest.raises(promodule.ProModuleError, match="not signed by SursumAI"):
        promodule.install()
    assert promodule.is_installed() is False


def test_a_tampered_archive_is_refused(served, monkeypatch):
    _pro_account(monkeypatch)
    signature = _sign(served["archive"])
    served["archive"] = _archive({"sursumai_pro/__init__.py": "import os  # surprise\n"})
    # the signature of the original, with the new bytes
    import core.promodule as pm
    monkeypatch.setattr(pm, "_fetch", lambda token, timeout=60: (served["archive"], signature, "1.0.0"))

    with pytest.raises(promodule.ProModuleError, match="not signed"):
        promodule.install()
    assert promodule.is_installed() is False


@pytest.mark.parametrize("name", [
    "../outside.py",
    "/etc/passwd",
    "sursumai_pro/../../escape.py",
])
def test_an_archive_that_writes_outside_its_directory_is_refused(served, monkeypatch, name):
    _pro_account(monkeypatch)
    served["archive"] = _archive({name: "boom\n"})

    with pytest.raises(promodule.ProModuleError, match="malformed"):
        promodule.install()
    assert not (promodule.PRO_DIR.parent / "outside.py").exists()


def test_a_failed_install_leaves_the_previous_module_alone(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    served["secret"] = OTHER            # the next download is not ours

    with pytest.raises(promodule.ProModuleError):
        promodule.install()

    assert promodule.is_installed() is True      # still the good one
    assert promodule.installed_version() == "1.0.0"


def test_a_pro_account_gets_the_new_module_on_its_own(served, monkeypatch):
    """A fix to the module must reach people who already pasted their token."""
    _pro_account(monkeypatch)
    promodule.install()
    served["version"] = "1.0.1"

    assert promodule.update() == "1.0.1"
    assert promodule.installed_version() == "1.0.1"


def test_updating_fails_quietly_and_keeps_what_is_there(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    served["secret"] = OTHER

    assert promodule.update() is None
    assert promodule.installed_version() == "1.0.0"


def test_a_free_account_downloads_nothing_on_update(served, monkeypatch):
    _pro_account(monkeypatch, pro=False)
    assert promodule.update() is None
    assert "token" not in served


def test_without_an_account_there_is_nothing_to_download(monkeypatch):
    monkeypatch.setattr(account, "token", lambda: None)
    with pytest.raises(promodule.ProModuleError, match="connect your SursumAI account"):
        promodule.install()


# ---- loading ----

def test_the_module_is_handed_the_running_app(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    app = object()

    assert promodule.load(app, store="store") is True

    import sursumai_pro
    assert sursumai_pro.REGISTERED == [(app, {"store": "store"})]


def test_loading_twice_registers_once(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    app = object()
    promodule.load(app, store="store")
    promodule.load(app, store="store")

    import sursumai_pro
    assert len(sursumai_pro.REGISTERED) == 1


def test_an_account_that_is_not_pro_loads_nothing(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    monkeypatch.setattr(account, "is_pro", lambda: False)

    assert promodule.load(object()) is False


def test_a_module_that_explodes_leaves_the_free_edition_working(served, monkeypatch, caplog):
    _pro_account(monkeypatch)
    served["archive"] = _archive(
        {"sursumai_pro/__init__.py": "raise RuntimeError('broken build')\n"})
    promodule.install()

    assert promodule.load(object()) is False      # and the app keeps running
    assert "failed to load" in caplog.text


def test_removing_it_goes_back_to_free(served, monkeypatch):
    _pro_account(monkeypatch)
    promodule.install()
    promodule.remove()

    assert promodule.is_installed() is False
    assert promodule.load(object()) is False


def test_installing_an_update_does_not_keep_running_the_old_code(served, monkeypatch):
    """Python caches a module by name: without forgetting it, an update would
    install and then quietly keep serving the previous version."""
    _pro_account(monkeypatch)
    promodule.install()
    served["archive"] = _archive({"sursumai_pro/__init__.py":
                                  "VERSION = 2\ndef register(app, **c): pass\n"})
    served["version"] = "2.0.0"
    promodule.install()
    assert promodule.load(object()) is True

    import sursumai_pro
    assert getattr(sursumai_pro, "VERSION", None) == 2
    assert promodule.installed_version() == "2.0.0"


def test_updating_while_it_is_already_serving_asks_for_a_restart(served, monkeypatch):
    """Routes already registered cannot be taken back, so say so instead of
    pretending the new version is live."""
    _pro_account(monkeypatch)
    promodule.install()
    promodule.load(object())
    served["version"] = "2.0.0"
    promodule.install()

    assert promodule.RESTART_NEEDED is True
