"""Who may be scanned, and how (B-S1).

Cloning, the structure graph and the SBOM run no repository code, so any public https repository on a known host
may be scanned *read-only*. Installing and testing code (fixes, probes) still needs EXEC_ALLOWED_REPOS.
"""
import re
from typing import Literal

from app.core.config import Settings
from app.core.workspace import repo_identity

Access = Literal["full", "read_only", "refused"]

# https only, no credentials in the link, owner and repository present.
_READ_ONLY_URL = re.compile(r"^https://(?:www\.)?(?:github\.com|bitbucket\.org)/[\w.-]+/[\w.-]+(?:\.git)?/?$")
READ_ONLY_NOTICE = "Read-only scan: Pilot will not install or test code in this repository."


def allowed_set(settings: Settings) -> set[str]:
    return {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}


def access_for(repo_url: str, settings: Settings) -> Access:
    url = repo_url.strip()
    if url.startswith("-"):
        return "refused"
    if repo_identity(url) in allowed_set(settings):
        return "full"
    return "read_only" if _READ_ONLY_URL.match(url) else "refused"


def read_only_scope(scope: dict[str, bool]) -> dict[str, bool]:
    """Nothing that runs code or reads production logs."""
    return {**scope, "fixes": False, "production_evidence": False}
