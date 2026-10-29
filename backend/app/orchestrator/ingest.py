"""ING-2: ingest(repo_url) -> scan_id.

Runs the same ingestion functions, in the same order, as app.main.process_github_url, but keeps the clone
(impact analysis and fix PRs need the code after the scan) and returns the scan id. Ingestion modules are
used as they are; nothing here edits them.
"""
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.core import db as core_db
from app.core.config import get_settings
from app.database import GraphDatabase
from app.dependency_extractor import extract_dependencies
from app.js_analyzer import analyze_js_code
from app.orchestrator import store as run_store
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


def canonical_repo_url(repo_url: str) -> str:
    """One spelling per repository, so re-scans, the push webhook and loop closure compare equal.

    `https://github.com/o/r.git`, `https://github.com/o/r/` and `https://github.com/o/r` are the same repository;
    a local path is kept as given (minus a trailing separator)."""
    url = repo_url.strip().rstrip("/\\")
    if "://" in url or url.startswith("git@"):
        url = url.removesuffix(".git")
    return url


def clone_key(repo_url: str) -> str:
    """`<owner>__<repo>` so org-a/api and org-b/api never share a folder."""
    parts = [p for p in re.split(r"[/\\:]+", repo_url.rstrip("/\\")) if p]
    owner = parts[-2] if len(parts) >= 2 else "local"
    key = f"{owner}__{repo_name(repo_url)}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def prune_clones(conn: sqlite3.Connection, workspace: str | Path) -> list[str]:
    """Delete clones that no unfinished run needs. The newest clone of each repo always stays: the next run uses it."""
    root = Path(workspace) / "clones"
    if not root.is_dir():
        return []
    in_use = {str(Path(p).resolve()) for p in run_store.clone_paths_in_use(conn)}
    removed: list[str] = []
    for repo_dir in (d for d in root.iterdir() if d.is_dir()):
        clones = sorted(d for d in repo_dir.iterdir() if d.is_dir())  # names are UTC stamps: oldest first
        for clone in clones[:-1]:
            if str(clone.resolve()) not in in_use:
                _remove_tree(clone)
                removed.append(str(clone))
    return removed


def _tree_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total / 1_048_576


def ingest(
    repo_url: str, db_path: str | None = None, workspace: str | Path | None = None, branch: str | None = None,
    *, limited: bool = False,
) -> str:
    """Clone `repo_url` into its own folder, scan it, and return the scan id.

    Every scan gets a fresh clone (the code stays identical to the scanned commit) in `clones/<owner>__<repo>/<stamp>`.
    Older clones stay while a run that started on them is unfinished: its worktree depends on them.
    `limited` (read-only scans of repositories nobody listed) caps clone time and size.
    """
    settings = get_settings()
    db_path = db_path or settings.codebase_pilot_db
    workspace = Path(workspace or settings.codebase_pilot_workspace)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    clone = (workspace / "clones" / clone_key(repo_url) / stamp).resolve()
    clone.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", *(["--branch", branch] if branch else []), "--", repo_url, str(clone)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=settings.scan_clone_timeout_s if limited else None,
        )
    except subprocess.TimeoutExpired as exc:
        if clone.exists():
            _remove_tree(clone)
        raise RuntimeError(f"git clone took longer than {settings.scan_clone_timeout_s} s and was stopped") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    if limited and (size := _tree_mb(clone)) > settings.scan_clone_max_mb:
        _remove_tree(clone)
        raise RuntimeError(f"repository is {size:.0f} MB; read-only scans stop at {settings.scan_clone_max_mb} MB")
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
                (scan_id, canonical_repo_url(repo_url), branch, str(clone)),
            )
        try:
            prune_clones(conn, workspace)
        except OSError:
            logger.warning("could not prune old clones", exc_info=True)
    finally:
        conn.close()
    logger.info("ingested %s as scan %s at %s", repo_url, scan_id, commit_sha[:7])
    return scan_id
