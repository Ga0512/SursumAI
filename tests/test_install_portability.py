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


def test_uv_is_put_on_the_path_before_it_is_looked_for():
    """It installs into ~/.local/bin, which the running shell does not have.
    Checking before adding it made a fresh Mac fall through to the stub at
    /usr/bin/python3, which opens the Xcode dialog and builds nothing."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = text[text.index("ensure_uv()"):text.index("PY=\"\"")]
    export_at = body.index('export PATH="$HOME/.local/bin')
    last_check = body.rindex("command -v uv")
    assert export_at < last_check, "the PATH must be set before the final check"


def test_the_mac_stub_is_explained_not_just_reported():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "xcode-select --install" in text, (
        "on macOS a venv fails because the command line tools are missing; "
        "the installer has to say so")


def test_the_command_does_not_depend_on_the_system_python():
    """`#!/usr/bin/env python3` on a Mac without the command line tools points
    at a stub that opens the Xcode dialog and runs nothing. The installed
    command has to call the environment the installer just built."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'ln -sf "$SURSUMAI_DIR/sursumai/bin/sursumai" "$BIN_DIR/sursumai"' not in text
    assert '.venv/bin/python" "$SURSUMAI_DIR/sursumai/bin/sursumai"' in text


def test_the_launcher_passes_the_arguments_through():
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    launcher = text[text.index("cat > \"$BIN_DIR/sursumai\""):text.index("LAUNCHER\nchmod")]
    assert r'"\$@"' in launcher, "without $@ the launcher swallows `status`, `update`…"


def test_the_old_symlink_is_removed_before_the_launcher_is_written():
    """`cat >` follows a symlink. An install before v1.0.23 left one here, so
    the launcher was written into the Python script it was meant to call, and
    the CLI died with "Missing parentheses in call to 'exec'"."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    remove_at = text.index('rm -f "$BIN_DIR/sursumai"')
    write_at = text.index('cat > "$BIN_DIR/sursumai"')
    assert remove_at < write_at, "the link has to go before anything is written"


def test_the_cli_script_is_still_python():
    """If an install ever writes the launcher over it, this is what notices."""
    script = (ROOT / "sursumai" / "bin" / "sursumai").read_text(encoding="utf-8")
    assert script.startswith("#!/usr/bin/env python3")
    assert "exec \"" not in script.splitlines()[1]


# ---- the checksum the installer could never find ----

SHA = "bd4ce4cc872c4a64a348e068d98e61b1ca3f2746b02af06b5287176af61f587f"


def _published_sha_lookup(sha256sums: str, asset: str) -> str:
    """Run the installer's OWN SHA256SUMS parser over a given file.

    The awk program is lifted out of install.sh rather than reimplemented
    here: a copy would keep passing while the shipped script failed, which is
    precisely the bug this guards against.
    """
    import re
    import shutil
    import subprocess

    if not shutil.which("awk"):
        pytest.skip("no awk on this machine")
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    match = re.search(r"awk -v a=\"\$SURSUMAI_ASSET\" '(.+?)'", text, re.S)
    assert match, "the SHA256SUMS parser is no longer where this test looks"
    done = subprocess.run(["awk", "-v", f"a={asset}", match.group(1)],
                          input=sha256sums, capture_output=True, text=True)
    return done.stdout.strip()


def test_the_binary_mode_format_is_read():
    """What every release actually published.

    `sha256sum -b` writes "<sha> *<name>", and the parser used to be
    `grep -F " $SURSUMAI_ASSET"` — a space followed by the name, which never
    matches a line where the space is followed by an asterisk. So `expected`
    stayed empty, the installer announced "no published checksum" and went on
    to install UNVERIFIED, on every release that shipped a SHA256SUMS. It hid
    for so long because a checksum that cannot be found is indistinguishable
    from one that was never published.
    """
    line = SHA + " *sursumai-1.0.27.tar.gz\n"
    assert _published_sha_lookup(line, "sursumai-1.0.27.tar.gz") == SHA


def test_the_text_mode_format_is_read():
    """What plain `sha256sum` writes: two spaces, no asterisk."""
    line = SHA + "  sursumai-1.0.27.tar.gz\n"
    assert _published_sha_lookup(line, "sursumai-1.0.27.tar.gz") == SHA


def test_a_different_assets_line_is_not_taken():
    """A release may publish more than one file. Reading the wrong line would
    abort every install with a mismatch instead of verifying anything."""
    body = (SHA + "  sursumai-1.0.26.tar.gz\n"
            + "0" * 64 + "  other.zip\n")
    assert _published_sha_lookup(body, "sursumai-1.0.27.tar.gz") == ""


def test_the_right_line_is_found_among_others():
    body = ("1" * 64 + "  other.zip\n"
            + SHA + " *sursumai-1.0.27.tar.gz\n")
    assert _published_sha_lookup(body, "sursumai-1.0.27.tar.gz") == SHA
