"""Step 8-9 of the loop: the PR is merged, the next scan sees the new version, the finding closes (G2)."""
import subprocess

import pytest
import respx

from app.core import db as core_db
from app.core import events
from app.orchestrator import push_tracker
from app.orchestrator import store as run_store
from tests.api.conftest import Env
from tests.e2e import golden

pytestmark = [pytest.mark.e2e, pytest.mark.node]


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout


@respx.mock
def test_merged_pr_resolves_the_run_on_the_next_scan(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    conn = core_db.connect(env.db)
    run = next(r for r in run_store.list_runs(conn) if r["package"] == "express")
    env.work(("resume_upgrade", {"run_id": run["id"], "decision": "approved", "decided_by": "alice"}))
    assert run_store.get_run(conn, run["id"])["status"] == "pr_opened"

    # A rescan before the merge changes nothing: express is still on 4.21.2.
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    assert run_store.get_run(conn, run["id"])["status"] == "pr_opened"

    # Someone merges the PR branch, then the next scan runs.
    git(env.origin, "merge", "--no-edit", "pilot/express-5.3.0")
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))

    closed = run_store.get_run(conn, run["id"])
    assert closed["status"] == "resolved"
    assert run_store.load_pull_request(conn, run["id"])["state"] == "merged"
    closure = [e for e in events.list_events(conn, run["id"]) if e.node == "closure"]
    assert len(closure) == 1 and "express is now 5.3.0" in closure[0].summary and "Resolved by commit" in closure[0].summary

    # The next run of the tracker is a no-op, and the run shows up as resolved in the API vocabulary.
    latest = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    assert push_tracker.close_resolved_runs(conn, latest) == []
    from app.api import queries

    assert queries.list_upgrades(conn, "resolved")[0]["status_label"] == "Resolved"
    assert "resolved" in queries.TERMINAL
    assert golden.EXPRESS_VERSIONS  # the registry fake knows 5.3.0, so the new scan has nothing left to upgrade


@respx.mock
def test_the_pr_is_only_called_merged_when_that_was_checked(tmp_path, monkeypatch):
    """B-11: a version bump that did not come through Pilot's PR is `superseded`, a closed GitHub PR is `closed`."""
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    conn = core_db.connect(env.db)
    run = next(r for r in run_store.list_runs(conn) if r["package"] == "express")
    env.work(("resume_upgrade", {"run_id": run["id"], "decision": "approved", "decided_by": "alice"}))

    assert push_tracker.pr_outcome(conn, run, None) == "superseded"  # the branch is not in the history
    assert push_tracker.pr_outcome(conn, run, {run["id"]: "closed"}) == "closed"
    assert push_tracker.pr_outcome(conn, run, {run["id"]: "merged"}) == "merged"
    assert push_tracker.pr_outcome(conn, run, {run["id"]: "open"}) == "superseded"
    git(env.origin, "merge", "--no-edit", "pilot/express-5.3.0")
    assert push_tracker.pr_outcome(conn, run, None) == "merged"  # now it is


@respx.mock
def test_closure_event_says_when_the_merge_was_not_verified(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    conn = core_db.connect(env.db)
    run = next(r for r in run_store.list_runs(conn) if r["package"] == "express")
    env.work(("resume_upgrade", {"run_id": run["id"], "decision": "approved", "decided_by": "alice"}))
    git(env.origin, "merge", "--no-edit", "pilot/express-5.3.0")
    git(env.origin, "branch", "-D", "pilot/express-5.3.0")  # the branch is gone: nothing to verify against
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    assert run_store.get_run(conn, run["id"])["status"] == "resolved"
    assert run_store.load_pull_request(conn, run["id"])["state"] == "superseded"
    closure = [e for e in events.list_events(conn, run["id"]) if e.node == "closure"]
    assert "not checked" in closure[0].summary
