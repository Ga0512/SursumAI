"""`sursumai uninstall`, and the button in Settings that runs it.

Nothing here touches the real machine: HOME, the install folder, docker and
pkill are all fakes.
"""
import argparse
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / "sursumai" / "bin" / "sursumai"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("sursumai_cli", str(CLI))
    spec = importlib.util.spec_from_loader("sursumai_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    home = tmp_path / "home"
    root = home / "sursumai"
    for p in [root / "llama-models" / "org--m", root / "sursumai-logs",
              home / ".sursumai" / "pro", home / ".local/share/sursumai",
              home / ".config/sursumai", home / ".local/bin", tmp_path / "logs"]:
        p.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text("1.0.4\n")
    (home / ".local/share/sursumai/sursumai.db").write_text("db")
    (home / ".local/bin/sursumai").write_text("#!/bin/sh\n")
    (home / ".bashrc").write_text("export PATH=...\n")
    (home / "notes.txt").write_text("mine\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(mod, "SURSUMAI_ROOT", root)
    monkeypatch.setattr(mod, "CONFIG_DIR", home / ".config/sursumai")
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    ran = []

    def _run(cmd, **kw):
        ran.append(cmd)
        out = ""
        if cmd[:2] == ["docker", "ps"]:
            out = ("aaa vllm/vllm-openai:v0.21.0\n"
                   "bbb ghcr.io/ggml-org/llama.cpp:server-cuda\n"
                   "ccc someone/else:latest\n")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    mod.ran, mod.home, mod.root = ran, home, root
    return mod


def _uninstall(cli, yes=True):
    cli.cmd_uninstall(argparse.Namespace(yes=yes))


def test_uninstall_removes_the_app_its_data_and_the_command(cli):
    _uninstall(cli)

    assert not cli.root.exists()                              # app + models
    assert not (cli.home / ".sursumai").exists()              # account, Pro module
    assert not (cli.home / ".local/share/sursumai").exists()  # the database
    assert not (cli.home / ".config/sursumai").exists()
    assert not (cli.home / ".local/bin/sursumai").exists()


def test_uninstall_leaves_everything_else_in_home_alone(cli):
    _uninstall(cli)
    assert (cli.home / "notes.txt").read_text() == "mine\n"
    assert (cli.home / ".bashrc").exists()


def test_running_models_are_stopped_but_only_ours(cli):
    """They outlive `sursumai stop`; left behind they keep the GPU busy."""
    _uninstall(cli)
    removed = [c for c in cli.ran if c[:3] == ["docker", "rm", "-f"]]
    assert removed == [["docker", "rm", "-f", "aaa", "bbb"]]


def test_the_three_processes_are_stopped(cli):
    _uninstall(cli)
    assert any(c[:2] == ["pkill", "-f"] for c in cli.ran)


def test_a_git_checkout_keeps_its_code(cli):
    """Someone developing SursumAI runs it from a clone: uninstalling must not
    delete their work."""
    (cli.root / ".git").mkdir()
    _uninstall(cli)
    assert (cli.root / "VERSION").exists()
    assert not (cli.home / ".sursumai").exists()


def test_without_typing_uninstall_nothing_is_removed(cli, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    _uninstall(cli, yes=False)
    assert cli.root.exists() and (cli.home / ".sursumai").exists()
    assert not any(c[:2] == ["docker", "rm"] for c in cli.ran)
