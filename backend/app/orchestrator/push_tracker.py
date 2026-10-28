"""G2: close the loop. After a re-scan, runs whose upgrade has landed become `resolved`.

A run is resolved when the newest scan of the same repository has the package at (or above) the version the
run upgraded to. The PR's state is only called `merged` when that was checked (GitHub says so, or the local
branch is in the repository's history). Otherwise it is `superseded`: the version arrived, but not provably
through this PR (Dependabot or a person may have bumped it). The `closure` event says which.
"""
import logging
import sqlite3
from pathlib import Path

from app.core import events, workspace
from app.orchestrator import store as run_store
from app.updates import store as updates_store
from app.updates.github import GitHubClient
from app.updates.versions import compare_versions

logger = logging.getLogger(__name__)


def _same_repo(conn: sqlite3.Connection, a: str, b: str) -> bool:
    ra, rb = run_store.scan_repo(conn, a)["repo_url"], run_store.scan_repo(conn, b)["repo_url"]
    return bool(ra) and ra == rb


async def fetch_pr_states(conn: sqlite3.Connection, client: GitHubClient) -> dict[str, str]:
    """run id -> 'merged' | 'closed' | 'open' for the open GitHub pull requests Pilot made (needs a token)."""
    states: dict[str, str] = {}
    for run in run_store.runs_with_status(conn, ("pr_opened", "approved")):
        pr = run_store.load_pull_request(conn, run["id"])
        if pr is None or pr["kind"] != "pull_request" or not pr["number"] or pr["state"] == "local":
            continue
        status, body = await client.request_json("GET", f"repos/{pr['repo']}/pulls/{pr['number']}")
        if status == 200 and isinstance(body, dict):
            states[run["id"]] = "merged" if body.get("merged") else body.get("state", "open")
    return states


def _local_state(conn: sqlite3.Connection, run, branch: str | None) -> str:
    """For a local pull request: is its branch part of the repository's current history?"""
    source = run_store.scan_repo(conn, run["scan_id"])
    repo = source["repo_url"] if source["repo_url"] and Path(source["repo_url"]).is_dir() else source["clone_path"]
    if not branch or not repo or not Path(repo).is_dir():
        return "superseded"
    check = workspace.git(repo, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD")
    return "merged" if check.passed else "superseded"


def pr_outcome(conn: sqlite3.Connection, run, pr_states: dict[str, str] | None) -> str:
    pr = run_store.load_pull_request(conn, run["id"])
    if pr is None:
        return "superseded"
    if pr_states and run["id"] in pr_states:
        return {"merged": "merged", "closed": "closed"}.get(pr_states[run["id"]], "superseded")
    if pr["state"] == "local":
        return _local_state(conn, run, pr["branch"])
    return "superseded"


def close_resolved_runs(
    conn: sqlite3.Connection, scan_id: str, pr_states: dict[str, str] | None = None
) -> list[str]:
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
        outcome = pr_outcome(conn, run, pr_states)
        run_store.mark_pull_request_state(conn, run["id"], outcome)
        note = {
            "merged": "",
            "closed": " (the pull request was closed without merging; the upgrade came from another change)",
            "superseded": " (not checked whether this pull request carried it; it may have come from another change)",
        }[outcome]
        events.emit(
            conn, run["id"], "closure", "finished",
            f"Resolved by commit {commit or 'unknown'}: {run['package']} is now {now['current_version']}{note}",
        )
        resolved.append(run["id"])
        logger.info("run %s resolved", run["id"], extra={"run_id": run["id"]})
    return resolved
