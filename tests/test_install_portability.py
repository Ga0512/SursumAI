"""The installer and start.sh have to work on a Mac too.

A real install on macOS died at `find -printf` — a GNU option BSD find does not
have — right after telling the user the checksum was fine. These tests read the
scripts for the handful of commands that only exist on Linux.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["install.sh", "start.sh", "setup.sh"]

# command -> what to use instead
GNU_ONLY = {
    "-printf": "find -printf is GNU; use `find ... | head -1` and dirname",
    "readlink -f": "readlink -f is GNU; use a shell loop or python",
    "sed -i ": "sed -i differs between GNU and BSD; write to a temp file",
    "grep -P": "grep -P is GNU; use grep -E",
    "stat -c": "stat -c is GNU; BSD uses stat -f",
}


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_gnu_only_commands(script):
    path = ROOT / script
    if not path.exists():
        pytest.skip(f"{script} does not exist")
    # comments are allowed to name the command — that is how we remember why
    linhas = [line for line in path.read_text(encoding="utf-8").splitlines()
              if not line.lstrip().startswith("#")]
    code = chr(10).join(linhas)
    for needle, why in GNU_ONLY.items():
        assert needle not in code, f"{script}: {why}"


def test_setsid_is_optional():
    """macOS has no setsid, and nohup alone detaches there."""
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "command -v setsid" in text, "start.sh must check for setsid before using it"
    assert "setsid nohup" not in text, "setsid must not be called unconditionally"


def test_the_checksum_falls_back_to_shasum():
    """macOS ships shasum, not sha256sum."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "sha256sum" in text and "shasum" in text
