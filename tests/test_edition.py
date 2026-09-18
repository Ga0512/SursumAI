"""One build, two editions.

The free edition and Pro are the same application; what differs is the
repository they update from, recorded by the installer in
``~/.sursumai/edition.json``. Getting this wrong is not cosmetic: a Pro
install that reads the public repository updates itself back to the free
edition and the buyer loses what they paid for.
"""
import json
from pathlib import Path

import pytest

from core import edition


@pytest.fixture
def edition_file(tmp_path, monkeypatch):
    path = tmp_path / ".sursumai" / "edition.json"
    monkeypatch.setattr(edition, "EDITION_FILE", path)
    for var in ("SURSUMAI_REPO", "SURSUMAI_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return path


def test_without_the_file_this_is_the_free_edition(edition_file):
    """Also the answer for an install made before the file existed."""
    assert edition.repo() == edition.PUBLIC_REPO
    assert edition.token() is None
    assert edition.is_pro() is False


def test_a_saved_token_makes_it_pro(edition_file):
    edition.save(edition.PRO_REPO, "ghp_secret")

    assert edition.repo() == edition.PRO_REPO
    assert edition.token() == "ghp_secret"
    assert edition.is_pro() is True
    assert edition.api_headers()["Authorization"] == "Bearer ghp_secret"


def test_the_token_file_is_not_readable_by_others(edition_file):
    edition.save(edition.PRO_REPO, "ghp_secret")
    assert oct(edition_file.stat().st_mode)[-3:] == "600"


def test_the_free_edition_sends_no_authorization_header(edition_file):
    edition.save(edition.PUBLIC_REPO, None)
    assert "Authorization" not in edition.api_headers()
    assert json.loads(edition_file.read_text()) == {"repo": edition.PUBLIC_REPO}


def test_the_environment_wins_over_the_file(edition_file, monkeypatch):
    """So a test or a support session can point one run somewhere else."""
    edition.save(edition.PRO_REPO, "from-file")
    monkeypatch.setenv("SURSUMAI_TOKEN", "from-env")
    assert edition.token() == "from-env"


# ---- the installer and the update paths agree with it ----

from pathlib import Path

INSTALL_SH = (Path(__file__).resolve().parent.parent / "install.sh").read_text()


def test_the_installer_chooses_the_repository_by_the_token():
    assert 'SURSUMAI_REPO="${SURSUMAI_REPO:-sursumai/SursumAI-Pro}"' in INSTALL_SH
    assert 'SURSUMAI_REPO="${SURSUMAI_REPO:-Ga0512/SursumAI}"' in INSTALL_SH


def test_the_installer_records_the_edition():
    """Without this an update of a Pro install comes back as the free one."""
    assert '"repo": "%s",\\n  "token": "%s"' in INSTALL_SH
    assert 'chmod 600 "$EDITION_FILE"' in INSTALL_SH


def test_a_pro_update_never_puts_the_token_in_a_command_line(monkeypatch, edition_file):
    """argv is readable by every process on the machine."""
    from central import app as central_app

    edition.save(edition.PRO_REPO, "ghp_secret")
    command, env = central_app._installer_command("v9.9.9")

    assert "ghp_secret" not in command
    assert env["SURSUMAI_TOKEN"] == "ghp_secret"
    assert edition.PRO_REPO in command                 # and it reads the private repo


def test_a_free_update_reads_the_public_repository(edition_file):
    from central import app as central_app

    command, env = central_app._installer_command("v9.9.9")
    assert edition.PUBLIC_REPO in command
    assert "Authorization" not in command
    assert "SURSUMAI_TOKEN" not in env
