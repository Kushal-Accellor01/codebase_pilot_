"""ING-2: ingest(repo_url) -> scan_id.

Runs the same ingestion functions, in the same order, as app.main.process_github_url, but keeps the clone
(impact analysis and fix PRs need the code after the scan) and returns the scan id. Ingestion modules are
used as they are; nothing here edits them.
"""
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from app.core import db as core_db
from app.core.config import get_settings
from app.database import GraphDatabase
from app.dependency_extractor import extract_dependencies
from app.js_analyzer import analyze_js_code
from app.python_analyzer import analyze_python_code
from app.resolver import resolve_imports
from app.scanner import scan_repository

logger = logging.getLogger(__name__)


def _remove_tree(path: Path) -> None:
    """Git marks object files read-only, which breaks rmtree on Windows."""

    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def repo_name(repo_url: str) -> str:
    return repo_url.rstrip("/\\").replace("\\", "/").split("/")[-1].removesuffix(".git") or "repo"


def ingest(repo_url: str, db_path: str | None = None, workspace: str | Path | None = None) -> str:
    """Clone `repo_url` into the workspace (replacing an older clone), scan it, and return the scan id."""
    settings = get_settings()
    db_path = db_path or settings.codebase_pilot_db
    workspace = Path(workspace or settings.codebase_pilot_workspace)
    clone = (workspace / repo_name(repo_url)).resolve()
    if clone.exists():
        _remove_tree(clone)  # a fresh clone keeps the code identical to the scanned commit
    clone.parent.mkdir(parents=True, exist_ok=True)

    result = _git("clone", "--depth", "1", repo_url, str(clone))
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    commit = _git("-C", str(clone), "rev-parse", "HEAD")
    commit_sha = commit.stdout.strip() if commit.returncode == 0 else "HEAD"
    branch = _git("-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None

    db = GraphDatabase(db_path)
    try:
        scan_id = scan_repository(str(clone), db, commit_sha)
        extract_dependencies(db, scan_id, str(clone))
        analyze_python_code(db, scan_id, str(clone))
        analyze_js_code(db, scan_id, str(clone))
        resolve_imports(db, scan_id)
        db.commit()
    finally:
        db.close()

    conn = core_db.connect(db_path)
    try:
        core_db.migrate(conn)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO scan_meta (scan_id, repo_url, branch, clone_path) VALUES (?, ?, ?, ?)",
                (scan_id, repo_url.rstrip("/"), branch, str(clone)),
            )
    finally:
        conn.close()
    logger.info("ingested %s as scan %s at %s", repo_url, scan_id, commit_sha[:7])
    return scan_id
