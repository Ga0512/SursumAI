"""The log level nobody could set, and the deploy log that never stopped.

`metrics` is pruned to METRICS_KEEP rows and has an index; the deploy log had
neither a cap nor a rotation, so a chatty runtime writing for weeks filled the
disk of the machine it was serving from. And nothing in the project ever
called logging.basicConfig, which made every log.debug in the codebase
unreachable: there was no way to ask a machine what it was doing.
"""

import logging

import pytest

from core import logs as logcfg


# ---- the level ----

def _reset_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_sursumai", False):
            root.removeHandler(h)


@pytest.fixture(autouse=True)
def _clean_root():
    _reset_root()
    before = logging.getLogger().level
    yield
    _reset_root()
    logging.getLogger().setLevel(before)


def test_the_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(logcfg.LEVEL_ENV, "debug")
    logcfg.setup("central")
    assert logging.getLogger("sursumai.central").isEnabledFor(logging.DEBUG)


def test_the_default_is_info(monkeypatch):
    monkeypatch.delenv(logcfg.LEVEL_ENV, raising=False)
    logcfg.setup("central")
    log = logging.getLogger("sursumai.central")
    assert log.isEnabledFor(logging.INFO)
    assert not log.isEnabledFor(logging.DEBUG)


def test_a_nonsense_level_does_not_stop_the_service(monkeypatch):
    """A typo in an env var must not be the reason a machine will not boot."""
    monkeypatch.setenv(logcfg.LEVEL_ENV, "verbose-please")
    logcfg.setup("agent")
    assert logging.getLogger("sursumai.agent").isEnabledFor(logging.INFO)


def test_setup_twice_does_not_double_every_line(monkeypatch):
    """uvicorn --reload imports the app twice, and two handlers on the root
    logger print everything twice."""
    monkeypatch.setenv(logcfg.LEVEL_ENV, "info")
    logcfg.setup("central")
    logcfg.setup("central")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_sursumai", False)]
    assert len(ours) == 1


# ---- the cap ----

def test_a_small_log_is_left_alone(tmp_path):
    path = tmp_path / "deploy.log"
    path.write_bytes(b"starting\n")
    assert logcfg.trim(path, max_bytes=1000, keep_bytes=500) == 9
    assert path.read_bytes() == b"starting\n"


def test_a_missing_log_is_not_an_error(tmp_path):
    """trim runs on every status poll, including for a deploy whose log has
    not been created yet."""
    assert logcfg.trim(tmp_path / "nope.log") == 0


def test_an_oversized_log_keeps_the_newest_lines(tmp_path):
    path = tmp_path / "deploy.log"
    lines = [f"line {i}\n".encode() for i in range(2000)]
    path.write_bytes(b"".join(lines))
    size = logcfg.trim(path, max_bytes=4000, keep_bytes=2000)
    body = path.read_bytes()
    assert size == len(body)
    assert size < 4000
    assert b"line 1999\n" in body, "the newest output is what matters"
    assert b"line 0\n" not in body
    assert body.startswith(b"[sursumai]"), "a trimmed log must say it was trimmed"


def test_a_trimmed_log_never_starts_mid_line(tmp_path):
    """Half a line at the top reads like corruption to whoever opens it."""
    path = tmp_path / "deploy.log"
    path.write_bytes(b"".join(f"a line that is fairly long: {i}\n".encode()
                             for i in range(500)))
    logcfg.trim(path, max_bytes=2000, keep_bytes=1000)
    kept = path.read_bytes().split(b"\n")
    # line 0 is the marker; every other line is whole
    for line in kept[1:-1]:
        assert line.startswith(b"a line that is fairly long: "), kept[1]


def test_trimming_is_idempotent(tmp_path):
    """The second pass must find the file already small, or a log would be
    rewritten on every one of the central's ten-second polls."""
    path = tmp_path / "deploy.log"
    path.write_bytes(b"".join(f"line {i}\n".encode() for i in range(2000)))
    first = logcfg.trim(path, max_bytes=4000, keep_bytes=2000)
    body = path.read_bytes()
    second = logcfg.trim(path, max_bytes=4000, keep_bytes=2000)
    assert second == first
    assert path.read_bytes() == body


def test_an_appending_writer_keeps_working_after_a_trim(tmp_path):
    """The runtime holds this file open in append mode. Append always writes
    at the end of the inode, which is why trimming in place is safe and a
    rename would not be: the process would go on writing to a file nobody
    reads while the new one stayed empty."""
    path = tmp_path / "deploy.log"
    path.write_bytes(b"".join(f"line {i}\n".encode() for i in range(2000)))
    with open(path, "ab") as writer:          # the runtime's fd
        logcfg.trim(path, max_bytes=4000, keep_bytes=2000)
        writer.write(b"after the trim\n")
        writer.flush()
    body = path.read_bytes()
    assert body.endswith(b"after the trim\n")
    assert b"line 1999\n" in body


# ---- wired into the poll ----

def test_the_agents_status_poll_is_what_caps_the_log(tmp_path, monkeypatch):
    """The cap has to live somewhere that runs without anyone watching. The
    central polls /deploys/{id}/status every 10 s for every live deploy, and
    that call already stats this file, so it is where the ceiling is free.
    """
    from agent import app as agent_app
    from agent import executor_llama

    path = tmp_path / "abcdef123456.log"
    path.write_bytes(b"".join(f"line {i}\n".encode() for i in range(20_000)))
    monkeypatch.setattr(executor_llama, "_log_file", lambda _id: path)
    monkeypatch.setattr(logcfg, "MAX_BYTES", 4000)
    monkeypatch.setattr(logcfg, "KEEP_BYTES", 2000)

    size = agent_app._log_size("abcdef123456")
    assert size == path.stat().st_size
    assert size < 4000, "the poll went past an oversized log without capping it"
    assert b"line 19999\n" in path.read_bytes()


def test_a_capped_log_still_reads_as_movement(tmp_path, monkeypatch):
    """The central watches (stage, log_bytes) for CHANGE, not for growth, to
    tell a slow start from a stalled one -- a 4 GiB download took 27 minutes
    and a fixed timeout failed it while it was writing progress. A trim makes
    the number drop, which is still a change, so a capped log must never look
    like a deploy that stopped moving.
    """
    from agent import app as agent_app
    from agent import executor_llama

    path = tmp_path / "abcdef123456.log"
    monkeypatch.setattr(executor_llama, "_log_file", lambda _id: path)
    monkeypatch.setattr(logcfg, "MAX_BYTES", 4000)
    monkeypatch.setattr(logcfg, "KEEP_BYTES", 2000)

    path.write_bytes(b"x" * 3000)
    before = agent_app._log_size("abcdef123456")
    with open(path, "ab") as f:                 # the runtime writes past the cap
        f.write(b"y" * 3000)
    after = agent_app._log_size("abcdef123456")
    assert after != before, "a trim that lands on the same size hides progress"
    assert after < before
