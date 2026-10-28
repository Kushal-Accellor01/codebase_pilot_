"""G2: close the loop. After a re-scan, runs whose upgrade has landed become `resolved`.

A run is resolved when the newest scan of the same repository has the package at (or above) the version the
run upgraded to. The PR is marked merged and a `closure` event records the commit that carried it.
"""
import logging
import sqlite3

from app.core import events
from app.orchestrator import store as run_store
from app.updates import store as updates_store
from app.updates.versions import compare_versions

logger = logging.getLogger(__name__)


def _same_repo(conn: sqlite3.Connection, a: str, b: str) -> bool:
    ra, rb = run_store.scan_repo(conn, a)["repo_url"], run_store.scan_repo(conn, b)["repo_url"]
    return bool(ra) and ra == rb


def close_resolved_runs(conn: sqlite3.Connection, scan_id: str) -> list[str]:
    """Mark runs from earlier scans of this repository as resolved when `scan_id` shows the new version."""
    resolved: list[str] = []
    commit = (run_store.scan_repo(conn, scan_id)["commit_sha"] or "")[:7]
    rows = {
        (r["ecosystem"], r["name"]): r for r in updates_store.list_dependency_updates(conn, scan_id)
        if r["current_version"]
    }
    for run in run_store.runs_with_status(conn, ("pr_opened", "approved")):
        if run["scan_id"] == scan_id or not run["to_version"] or not _same_repo(conn, run["scan_id"], scan_id):
            continue
        now = rows.get((run["ecosystem"], run["package"]))
        if now is None or compare_versions(now["current_version"], run["to_version"]) < 0:
            continue
        run_store.set_run_status(conn, run["id"], "resolved", node="closure")
        run_store.mark_pull_request_state(conn, run["id"], "merged")
        events.emit(
            conn, run["id"], "closure", "finished",
            f"Resolved by commit {commit or 'unknown'}: {run['package']} is now {now['current_version']}",
        )
        resolved.append(run["id"])
        logger.info("run %s resolved", run["id"], extra={"run_id": run["id"]})
    return resolved
