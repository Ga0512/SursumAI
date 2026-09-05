"""Things that break the install without ever raising an exception.

SursumAI runs from bash on Linux/WSL. A CRLF in a shell script or in the CLI's
shebang fails with "bad interpreter" and no useful message — and it is easy to
introduce from a Windows editor, so it is checked here rather than discovered
by a user.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = ["install.sh", "setup.sh", "start.sh", "release.sh"]
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gz", ".zip", ".gguf"}


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:  # not a git checkout (e.g. an installed tarball)
        pytest.skip("not a git checkout")
    for name in out.stdout.split():
        path = ROOT / name
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES:
            yield name, path


def test_no_tracked_file_uses_windows_line_endings():
    offenders = [name for name, path in _tracked_text_files()
                 if b"\r\n" in path.read_bytes()]
    assert not offenders, f"CRLF line endings in: {offenders}"


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_the_shell_scripts_parse(script):
    # relative name + cwd: bash here may be a POSIX shell that cannot read a
    # Windows-style absolute path
    result = subprocess.run(["bash", "-n", script], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode == 127:
        pytest.skip("no usable bash on this machine")
    assert result.returncode == 0, result.stderr


def test_the_cli_has_a_clean_shebang():
    first = (ROOT / "sursumai" / "bin" / "sursumai").read_bytes().split(b"\n")[0]
    assert first == b"#!/usr/bin/env python3"


def test_the_cli_is_valid_python():
    import ast

    source = (ROOT / "sursumai" / "bin" / "sursumai").read_text(encoding="utf-8")
    ast.parse(source)


def test_the_pinned_installer_tag_matches_the_version_file():
    """`sursumai update` and the README both install a tag — a VERSION bump
    that forgets install.sh would ship the previous release forever."""
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert f'SURSUMAI_VERSION="${{SURSUMAI_VERSION:-v{version}}}"' in installer


def test_the_readme_installs_the_pinned_tag_not_a_branch():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"/raw/v{version}/install.sh" in readme
    assert "/raw/main/install.sh" not in readme


def test_every_requirement_is_pinned():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "==" in line, f"unpinned requirement: {line}"


def test_the_installer_downloads_the_release_asset_not_a_source_archive():
    """GitHub's generated archives are not byte-stable; the installer aborts on
    a checksum mismatch, so it must fetch the immutable uploaded asset."""
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'SURSUMAI_ASSET="sursumai-${SURSUMAI_VERSION#v}.tar.gz"' in installer
    assert "$RELEASE_BASE/$SURSUMAI_ASSET" in installer


def test_the_release_script_builds_the_asset_the_installer_expects():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    release = (ROOT / "release.sh").read_text(encoding="utf-8")
    assert 'ASSET="sursumai-$VERSION.tar.gz"' in release
    assert version  # the script derives everything from VERSION


def test_the_project_ships_a_license():
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").strip()
