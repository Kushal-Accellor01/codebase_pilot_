"""Shared fixtures: the sandbox app as a throw-away git repository."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "pilot-sandbox-shop"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_sandbox_repo(dest: Path) -> Path:
    """Copy pilot-sandbox-shop (without node_modules) into `dest` and commit it."""
    shutil.copytree(SANDBOX, dest, ignore=shutil.ignore_patterns("node_modules"))
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "tester")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "sandbox")
    return dest


@pytest.fixture(scope="session")
def sandbox_origin(tmp_path_factory) -> Path:
    """A local git repository with the sandbox app, usable as a clone source."""
    return make_sandbox_repo(tmp_path_factory.mktemp("origin") / "pilot-sandbox-shop")
