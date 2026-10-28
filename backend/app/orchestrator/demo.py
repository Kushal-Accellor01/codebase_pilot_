"""Demo housekeeping: put the system back to "scanned, nothing decided yet" (reset-demo)."""
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

from app.core import db as core_db
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

RUN_TABLES = (
    "run_events", "impact_findings", "telemetry_evidence", "remediation_plans", "validations", "patches",
    "approvals", "pull_requests", "feedback", "upgrade_runs", "jobs",
)


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)


def delete_pilot_branches(origin: str) -> list[str]:
    """Remove `pilot/*` branches from a local origin repository (the offline demo's PR branches)."""
    if not Path(origin).is_dir():
        return []
    branches = [b.strip().lstrip("* ") for b in _git(origin, "branch", "--list", "pilot/*").stdout.splitlines()]
    for branch in branches:
        _git(origin, "branch", "-D", branch)
    return [b for b in branches if b]


def reset_demo(db_path: str | None = None, settings: Settings | None = None) -> dict:
    """Delete run data, checkpoints, worktrees, local PR branches and report files. Scans and Step 2 results stay."""
    settings = settings or get_settings()
    db_path = db_path or settings.codebase_pilot_db
    summary: dict = {"tables": [], "branches": [], "files": 0}
    if Path(db_path).is_file():
        conn = core_db.connect(db_path)
        try:
            names = core_db.table_names(conn)
            origins = [r["repo_url"] for r in conn.execute("SELECT DISTINCT repo_url FROM scan_meta")] if (
                "scan_meta" in names) else []
            clones = [r["clone_path"] for r in conn.execute("SELECT DISTINCT clone_path FROM scan_meta")] if (
                "scan_meta" in names) else []
            with conn:
                for table in RUN_TABLES:
                    if table in names:
                        conn.execute(f"DELETE FROM {table}")  # noqa: S608
                        summary["tables"].append(table)
            for origin in origins:
                summary["branches"] += delete_pilot_branches(origin) if origin else []
            for clone in clones:
                if clone and Path(clone).is_dir():
                    _git(clone, "worktree", "prune")
        finally:
            conn.close()

    runs_dir = Path(settings.codebase_pilot_workspace) / "runs"
    if runs_dir.exists():
        shutil.rmtree(runs_dir, ignore_errors=True)
        summary["files"] += 1
    for suffix in ("", "-wal", "-shm"):
        path = Path(settings.checkpoint_db + suffix)
        if path.exists():
            path.unlink()
            summary["files"] += 1
    reports = Path(settings.reports_dir)
    for pattern in ("pr_*.md", "issue_*.md"):
        for path in reports.glob(pattern):
            path.unlink()
            summary["files"] += 1
    return summary


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = core_db.table_names(conn)
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in RUN_TABLES if t in names}  # noqa: S608
