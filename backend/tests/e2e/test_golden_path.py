"""The whole loop offline: mirror -> scan -> updates -> impact -> plan -> fix -> approval -> local PR."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
import respx

from app.core import db as core_db
from app.core import events, jobs
from app.orchestrator import cli as orch_cli
from app.orchestrator import runner
from app.orchestrator import store as run_store
from app.orchestrator.worker import Services, run_worker
from app.updates import store as updates_store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from tests.conftest import make_sandbox_repo
from tests.e2e import golden
from tests.e2e.golden import ScriptedLLM

pytestmark = [pytest.mark.e2e, pytest.mark.node]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals"))
from run_eval import evaluate, load_key  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    sandbox_origin = make_sandbox_repo(tmp_path / "origin" / "pilot-sandbox-shop")  # fresh per test: PRs push branches
    db = tmp_path / "demo.db"
    monkeypatch.setenv("CODEBASE_PILOT_DB", str(db))
    monkeypatch.setenv("CODEBASE_PILOT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "ck.db"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", str(sandbox_origin))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SEEDED_LOGS_DIR", raising=False)
    golden.install_fake_runner(monkeypatch)
    orch_cli._ensure_db(str(db))
    return {"db": str(db), "origin": sandbox_origin, "tmp": tmp_path}


def services(http, llm) -> Services:
    return Services(
        http=http, registry=golden.registry(), github=GitHubClient(http, "t"), llm=llm,
        update_options={"security": False, "write_report": False},
    )


def go(env, llm, *jobs_to_add):
    async def main():
        async with HttpClient(wait_scale=0) as http:
            conn = core_db.connect(env["db"])
            for kind, payload in jobs_to_add:
                jobs.enqueue(conn, kind, payload)
            conn.close()
            return await run_worker(env["db"], until_idle=True, services=services(http, llm))

    return asyncio.run(main())


def scan_job(env):
    return ("scan", {"repo_url": str(env["origin"]), "only": ["express"]})


def decide_job(run_id, decision="approved"):
    return ("resume_upgrade", {"run_id": run_id, "decision": decision, "decided_by": "alice", "comment": "ship it"})


def express_run(conn):
    return next(r for r in run_store.list_runs(conn) if r["package"] == "express")


@respx.mock
def test_golden_path_scan_to_local_pull_request(env):
    golden.mock_github()
    llm = ScriptedLLM()
    handled = go(env, llm, scan_job(env))
    assert handled == 3  # scan, dependency_updates, one upgrade_run

    conn = core_db.connect(env["db"])
    run = express_run(conn)
    assert run["status"] == "awaiting_approval" and run["risk_level"] in ("high", "critical")
    assert (run["from_version"], run["to_version"]) == ("4.21.2", "5.3.0")

    # Step 2 result behind the run: validated breaking changes with quotes from the fetched notes
    scan = updates_store.latest_scan_id(conn)
    row = {r["name"]: r for r in updates_store.list_dependency_updates(conn, scan)}["express"]
    assert row["status"] == "analyzed" and row["current_version_basis"] == "lockfile"
    changes = updates_store.load_changes(conn, row["analysis_id"])
    assert len(changes) >= 5 and all(c["breaking"] for c in changes)

    # Impact: every planted line is found; the negative control never is
    findings = run_store.load_findings(conn, run["id"])
    result = evaluate(findings, load_key())
    assert result["recall"] == 1.0, result["missed"]
    assert result["precision"] >= 0.9, result["extra"]
    assert result["negatives_reported"] == []
    by_line = {(f.file_path, f.line): f for f in findings}
    assert {"pattern", "runtime_deprecation", "test_failure"} <= set(by_line[("routes/orders.js", 8)].sources)
    assert "pattern" not in by_line[("routes/invoices.js", 11)].sources  # found by runtime + test evidence alone

    # Production evidence, plan, patch and validation
    assert {t.related_symbol for t in run_store.load_telemetry(conn, run["id"])} >= {"req.param", "res.sendfile"}
    plan = run_store.load_plan(conn, run["id"])
    assert plan.severity == "critical" and plan.effort_hours > 0
    patch = run_store.load_patches(conn, run["id"])[-1]
    assert patch["validation"]["passed"] == 1 and "req.params.id" in patch["diff"] and patch["manifest_change"]
    assert patch["validation"]["tests_passed"] == 9
    assert len(patch["edits"]) == 7 and patch["rejected"] == []

    # The trace
    nodes_seen = [e.node for e in events.list_events(conn, run["id"]) if e.status in ("finished", "waiting")]
    assert nodes_seen[:4] == ["prepare", "impact", "baseline_probe", "upgrade_probe"] or "plan" in nodes_seen
    assert events.list_events(conn, run["id"])[-1].status == "waiting"
    assert run_store.load_pull_request(conn, run["id"]) is None  # nothing leaves the system before approval
    assert (env["tmp"] / "ws" / "runs" / run["id"]).exists()  # the worktree is kept while waiting

    # Approve -> pull request through the local fallback
    go(env, llm, decide_job(run["id"]))
    run = run_store.get_run(conn, run["id"])
    assert run["status"] == "pr_opened"
    pr = run_store.load_pull_request(conn, run["id"])
    assert pr["kind"] == "pull_request" and pr["state"] == "local" and pr["branch"] == "pilot/express-5.3.0"
    body = Path(pr["url"]).read_text(encoding="utf-8")
    assert f"codebase-pilot:run={run['id']}" in body and "Approved by alice" in body
    assert "req.param" in body and "release notes" in body and "@acme/team-shop" in body
    branches = subprocess.run(["git", "-C", str(env["origin"]), "branch", "--list", "pilot/*"],
                              capture_output=True, text=True).stdout
    assert "pilot/express-5.3.0" in branches
    shown = subprocess.run(["git", "-C", str(env["origin"]), "show", "pilot/express-5.3.0:routes/orders.js"],
                           capture_output=True, text=True).stdout
    assert "req.params.id" in shown and "req.param(" not in shown
    assert not (env["tmp"] / "ws" / "runs" / run["id"]).exists()  # worktree cleaned up at the end
    approval = run_store.load_approval(conn, run["id"])
    assert (approval["decision"], approval["decided_by"], approval["comment"]) == ("approved", "alice", "ship it")


@respx.mock
def test_second_pass_changes_nothing(env):
    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    run_id = express_run(conn)["id"]
    go(env, llm, decide_job(run_id))
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("upgrade_runs", "impact_findings", "patches", "pull_requests", "update_analyses", "jobs")}
    llm_calls = llm.calls

    # Same scan again: the re-run must not duplicate rows, PRs, or LLM work.
    scan = updates_store.latest_scan_id(conn)
    assert runner.enqueue_runs_for_scan(conn, scan, only=["express"]) == [run_id]
    go(env, llm, ("dependency_updates", {"scan_id": scan, "only": ["express"]}))
    after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ("upgrade_runs", "impact_findings", "patches", "pull_requests", "update_analyses")}
    assert after == {k: v for k, v in before.items() if k != "jobs"}
    assert llm.calls == llm_calls  # analysis came from the cache
    assert run_store.get_run(conn, run_id)["status"] == "pr_opened"


@respx.mock
def test_rescan_while_paused_keeps_the_run_and_approval_still_opens_the_pr(env):
    """H1 + H2: a second scan neither breaks the paused run's worktree nor creates a second express run."""
    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    run_id = express_run(conn)["id"]
    first_clone = run_store.scan_repo(conn, run_store.get_run(conn, run_id)["scan_id"])["clone_path"]
    calls = llm.calls

    go(env, llm, scan_job(env))
    assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 2
    assert [r["id"] for r in run_store.list_runs(conn) if r["package"] == "express"] == [run_id]
    assert llm.calls == calls  # no second analysis, impact or fix
    assert Path(first_clone).is_dir()  # the paused run's clone survives the re-scan
    assert run_store.get_run(conn, run_id)["status"] == "awaiting_approval"

    go(env, llm, decide_job(run_id))
    assert run_store.get_run(conn, run_id)["status"] == "pr_opened"
    assert run_store.load_pull_request(conn, run_id)["branch"] == "pilot/express-5.3.0"

    go(env, llm, scan_job(env))  # the run is final now: its old clone is pruned, the newest stays
    clones = sorted((env["tmp"] / "ws" / "clones").glob("*/*"))
    assert len(clones) == 1 and str(clones[0].resolve()) != str(Path(first_clone).resolve())


@respx.mock
def test_rejection_ends_without_a_pull_request(env):
    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    run_id = express_run(conn)["id"]
    go(env, llm, decide_job(run_id, "rejected"))
    assert run_store.get_run(conn, run_id)["status"] == "rejected"
    assert run_store.load_pull_request(conn, run_id) is None
    assert "pilot/" not in subprocess.run(["git", "-C", str(env["origin"]), "branch"], capture_output=True, text=True).stdout


@respx.mock
def test_deciding_twice_is_a_conflict(env):
    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    run_id = express_run(conn)["id"]
    go(env, llm, decide_job(run_id))

    async def again():
        async with HttpClient(wait_scale=0) as http:
            ctx = runner.make_context(conn, http, llm=llm)
            await runner.resume_run(ctx, run_id, "approved", "bob")

    with pytest.raises(runner.RunConflict):
        asyncio.run(again())
    assert run_store.load_approval(conn, run_id)["decided_by"] == "alice"


@respx.mock
def test_repository_outside_the_allowlist_gets_a_report_only_run(env, monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "someone/else")
    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    run = express_run(conn)
    assert run["status"] == "report_only"
    assert run_store.load_plan(conn, run["id"]) is not None
    assert run_store.load_patches(conn, run["id"]) == []  # no code was run, so no fix was attempted
    findings = run_store.load_findings(conn, run["id"])
    assert findings and all(f.sources == ["pattern"] for f in findings)  # static evidence only


@respx.mock
def test_reset_demo_clears_runs_branches_and_checkpoints(env):
    from app.orchestrator import demo

    golden.mock_github()
    llm = ScriptedLLM()
    go(env, llm, scan_job(env))
    conn = core_db.connect(env["db"])
    go(env, llm, decide_job(express_run(conn)["id"]))
    summary = demo.reset_demo(env["db"])
    assert "pilot/express-5.3.0" in summary["branches"] and "upgrade_runs" in summary["tables"]
    conn = core_db.connect(env["db"])
    assert demo.counts(conn)["upgrade_runs"] == 0 and demo.counts(conn)["jobs"] == 0
    assert updates_store.latest_scan_id(conn) is not None  # scans and Step 2 results stay
    assert not (env["tmp"] / "ck.db").exists()
    assert "pilot/" not in subprocess.run(["git", "-C", str(env["origin"]), "branch"], capture_output=True, text=True).stdout
    json.dumps(summary)
