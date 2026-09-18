"""Which SursumAI this is, and where it updates from.

There is one build. The free edition installs from the public repository; Pro
installs the same application from a private one, and the only difference at
runtime is where updates come from. That is written once by the installer to
``~/.sursumai/edition.json`` (0600, it holds a GitHub token) and read by the
update check, the CLI and the dashboard.

Without the file this is the free edition — which is also what an old install
that predates this file looks like, and is the right answer for it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PUBLIC_REPO = "Ga0512/SursumAI"
PRO_REPO = "sursumai/SursumAI-Pro"

EDITION_FILE = Path(os.path.expanduser("~/.sursumai/edition.json"))


def _read() -> dict:
    try:
        data = json.loads(EDITION_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def repo() -> str:
    """The GitHub repository this install updates from."""
    return os.environ.get("SURSUMAI_REPO") or _read().get("repo") or PUBLIC_REPO


def token() -> str | None:
    """The token that reads that repository, for Pro. None for the free one."""
    return os.environ.get("SURSUMAI_TOKEN") or os.environ.get("GITHUB_TOKEN") or _read().get("token")


def is_pro() -> bool:
    return repo() != PUBLIC_REPO


def name() -> str:
    return "SursumAI Pro" if is_pro() else "SursumAI"


def api_headers() -> dict[str, str]:
    """Headers for api.github.com. A private repository answers 404 without
    the token — the same as a repository that does not exist — so a Pro
    install with an expired token must not read that as "no updates"."""
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "sursumai"}
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def save(repo_name: str, tok: str | None) -> None:
    """Record the edition after an install. Only the installer calls this."""
    EDITION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"repo": repo_name}
    if tok:
        data["token"] = tok
    EDITION_FILE.write_text(json.dumps(data, indent=2) + "\n")
    EDITION_FILE.chmod(0o600)
