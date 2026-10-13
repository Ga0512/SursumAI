"""Run tests/app_sendplay.js -- the dashboard's chat senders, executed for real.

Kept in the pytest suite so that `python -m pytest` covers the front-end too:
the bug it was written for (an unbound `isPool` in the deploy's Test tab, which
turned a model answering in 1.7 s into "Could not reach server") was invisible
to `node --check` and to every Python test, and shipped for that reason.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "app_sendplay.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_dashboard_chat_senders_run():
    proc = subprocess.run([shutil.which("node"), str(HARNESS)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
