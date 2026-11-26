"""`sursumai service`: the thing that keeps it running.

Nothing did. A reboot stopped SursumAI, a crash stopped it, and `sursumai
update` stops the three processes and ends by telling you to start them again
— so the API was down from the update until somebody noticed.

These tests read the generated launchd plist and systemd unit without
installing anything: whether a unit file is correct is decidable from its
text, and this suite runs on a machine that has neither manager.
"""

import importlib.util
import plistlib
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(monkeypatch):
    """The CLI script, imported as a module."""
    spec = importlib.util.spec_from_loader(
        "sursumai_cli",
        importlib.machinery.SourceFileLoader(
            "sursumai_cli", str(ROOT / "sursumai" / "bin" / "sursumai")),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the three units ----

def test_one_unit_per_process(cli):
    """A supervisor supervises one process. A single unit that starts three
    and exits survives a reboot but never notices a crash, which is half the
    reason to run a service at all."""
    assert set(cli._service_commands()) == {"web", "central", "agent"}


def test_the_service_starts_things_the_same_way_the_cli_does(cli):
    """A service file that drifts from how the CLI starts things works right
    up until it is the only thing starting them."""
    commands = cli._service_commands()
    assert commands["central"][1:3] == ["-m", "uvicorn"]
    assert "central.app:app" in commands["central"]
    assert "web/server.py" in commands["web"]
    for command in commands.values():
        assert command[0].endswith(".venv/bin/python"), "must not use the system python"


# ---- launchd ----

def _plist_of(cli, name="central", env=None):
    command = cli._service_commands()[name]
    return plistlib.loads(cli._plist(name, command, env or {}).encode())


def test_the_plist_is_valid_and_restarts_on_crash(cli):
    data = _plist_of(cli)
    assert data["Label"] == "com.sursumai.central"
    assert data["RunAtLoad"] is True, "would not come back after a reboot"
    assert data["KeepAlive"] is True, "would not come back after a crash"
    assert data["WorkingDirectory"] == str(ROOT)


def test_the_plist_carries_the_configuration(cli):
    """The actual pain this fixes: SURSUMAI_BIND as an `export` in one shell
    is how an exposed install silently goes back to loopback after a reboot,
    and the API stops answering on the address people were given."""
    data = _plist_of(cli, env={"SURSUMAI_BIND": "0.0.0.0", "SURSUMAI_LOG": "debug"})
    assert data["EnvironmentVariables"]["SURSUMAI_BIND"] == "0.0.0.0"
    assert data["EnvironmentVariables"]["SURSUMAI_LOG"] == "debug"


def test_the_plist_escapes_xml(cli):
    """A path or value with an ampersand in it would produce a plist launchd
    refuses to parse, and the failure would arrive at the next reboot."""
    data = _plist_of(cli, env={"SURSUMAI_CORS_ORIGINS": "https://a.com,https://b.com?x=1&y=2"})
    assert data["EnvironmentVariables"]["SURSUMAI_CORS_ORIGINS"].endswith("x=1&y=2")


# ---- systemd ----

def test_the_unit_restarts_always(cli):
    unit = cli._unit("central", cli._service_commands()["central"], {})
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit, "would not start at boot"
    assert f"WorkingDirectory={ROOT}" in unit


def test_the_unit_quotes_the_command(cli):
    """An install path with a space in it would otherwise be split into
    separate arguments by systemd."""
    command = [str(ROOT / "a dir" / "python"), "-m", "uvicorn"]
    unit = cli._unit("central", command, {})
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert shlex.split(line[len("ExecStart="):])[0].endswith("a dir/python")


def test_the_unit_carries_the_configuration(cli):
    unit = cli._unit("web", cli._service_commands()["web"],
                     {"SURSUMAI_BIND": "0.0.0.0"})
    assert "Environment=SURSUMAI_BIND=0.0.0.0" in unit


def test_the_central_is_ordered_after_the_agent_but_does_not_require_it(cli):
    """Ordering only: all three retry, and a hard requirement would mean one
    failing unit keeps the others down."""
    unit = cli._unit("central", cli._service_commands()["central"], {})
    assert "After=sursumai-agent.service" in unit
    assert "Requires=" not in unit


# ---- what must never reach a unit file ----

def test_no_secret_is_written_into_a_unit(cli, monkeypatch):
    """AGENT_KEY lives in ~/.sursumai/agent.key at 0600 and is read from
    there. Baking it into a unit file would be a second copy of a secret, in
    a file whose whole purpose is to be read by a service manager."""
    monkeypatch.setenv("AGENT_KEY", "super-secret-value")
    monkeypatch.setenv("SURSUMAI_API_TOKEN", "another-secret")
    monkeypatch.setenv("SURSUMAI_SIGNING_KEY", "third-secret")
    monkeypatch.setenv("SURSUMAI_BIND", "0.0.0.0")
    env = cli._service_env()
    blob = cli._unit("central", ["python"], env) + cli._plist("central", ["python"], env)
    for secret in ("super-secret-value", "another-secret", "third-secret"):
        assert secret not in blob, "a secret was written into a service file"
    assert env["SURSUMAI_BIND"] == "0.0.0.0", "the configuration still has to be carried"


def test_only_sursumai_configuration_is_carried(cli, monkeypatch):
    """A unit that inherited the whole environment of whatever shell ran the
    install would behave differently from the same install done elsewhere."""
    monkeypatch.setenv("UNRELATED_THING", "value")
    monkeypatch.setenv("SURSUMAI_LOG", "debug")
    env = cli._service_env()
    assert "UNRELATED_THING" not in env
    assert env["SURSUMAI_LOG"] == "debug"


def test_windows_is_refused_with_a_sentence(cli, monkeypatch):
    """The three services are Linux processes; on Windows this has to be run
    from WSL, and saying so beats a traceback about launchctl."""
    monkeypatch.setattr(cli.sys, "platform", "win32")
    with pytest.raises(cli.CliError, match="WSL"):
        cli._service_install()
