import os
import sys
import tempfile
from pathlib import Path

# import the project, not an installed copy
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Never touch the user's real key file or database from a test run.
os.environ.setdefault("AGENT_KEY", "test-agent-key")
os.environ.setdefault("SURSUMAI_HOME", tempfile.mkdtemp(prefix="sursumai-home-"))
os.environ.setdefault("SURSUMAI_DB_DIR", tempfile.mkdtemp(prefix="sursumai-db-"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_gpu_or_docker_unless_a_test_says_so(monkeypatch):
    """Whether this machine has an NVIDIA card must not decide a test — a test
    that depended on the machine already blocked a release once. Tests that
    care set it explicitly with monkeypatch; the rest see a machine without a
    GPU. It also keeps the suite from shelling out to nvidia-smi, which on some
    machines waits the full 10 s timeout on every call."""
    from agent import executor_llama
    monkeypatch.setattr(executor_llama, "_gpu_available", lambda: False)
    # same for Docker: `docker info` can wait out its timeout, and whether
    # Docker Desktop happens to be running is not something a test asserts on
    monkeypatch.setattr(executor_llama, "_docker_here", lambda: False)
