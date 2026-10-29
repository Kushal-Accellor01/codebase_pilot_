"""S2 and S3: a scan request with scope and stages, results, vulnerabilities and the SBOM download."""
import json
import os

import pytest

from app.core import db as core_db
from app.scan import store as scan_store
from tests.api.test_api import client_for, read_sse

pytestmark = [pytest.mark.node]
STAGES = ["clone", "layer1", "sbom", "internal", "layer2", "vulnerabilities", "versions", "changes", "impact"]


def allowed(env) -> dict:
    return {"exec_allowed_repos": str(env.origin)}


def start(client, env, **body):
    r = client.post("/api/scans", json={"repo_url": str(env.origin), "only": ["express"], **body})
    assert r.status_code == 202, r.text
    return r.json()


def stages(client, request_id) -> dict:
    return {s["stage"]: s for s in client.get(f"/api/scan-requests/{request_id}").json()["stages"]}


# --- the request ---------------------------------------------------------------------------------------------------


def test_request_is_stored_with_scope_branch_and_job(tmp_path, monkeypatch):
    from tests.api.conftest import Env

    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    with client_for(env, **allowed(env)) as client:
        scope = {"vulnerabilities": True, "breaking_changes": False, "internal_contracts": False,
                 "production_evidence": False, "fixes": False, "sbom": True}
        accepted = start(client, env, branch="main", scope=scope)
        detail = client.get(f"/api/scan-requests/{accepted['scan_request_id']}").json()
        assert detail["status"] == "queued" and detail["branch"] == "main" and detail["scope"] == scope
        assert [s["stage"] for s in detail["stages"]] == STAGES and all(s["status"] == "queued" for s in detail["stages"])
        conn = core_db.connect(env.db)
        job = conn.execute("SELECT payload FROM jobs WHERE id = ?", (accepted["job_id"],)).fetchone()
        payload = json.loads(job["payload"])
        assert payload["request_id"] == accepted["scan_request_id"] and payload["scope"] == scope
        assert payload["branch"] == "main"


@pytest.mark.parametrize("branch", ["--upload-pack=evil", "-x", "a b", "a;b", "", "x" * 101])
def test_a_branch_can_never_be_read_as_an_option(tmp_path, monkeypatch, branch):
    from tests.api.conftest import Env

    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    with client_for(env, **allowed(env)) as client:
        r = client.post("/api/scans", json={"repo_url": str(env.origin), "branch": branch})
        assert r.status_code == 422


def test_unknown_request_is_404_and_repo_outside_the_allowlist_is_403(fresh):
    with client_for(fresh, **allowed(fresh)) as client:
        assert client.get("/api/scan-requests/nope").status_code == 404
        assert client.get("/api/scan-requests/nope/events").status_code == 404
        assert client.post("/api/scan-requests/nope/cancel").status_code == 404
        assert client.post("/api/scans", json={"repo_url": "https://github.com/evil/repo"}).status_code == 403


def test_repo_check_reports_allowlist_and_access(fresh):
    with client_for(fresh, **allowed(fresh)) as client:
        ok = client.post("/api/scans/check", json={"repo_url": str(fresh.origin)}).json()
        assert ok["allowed"] and ok["ok"] and ok["provider"] == "local" and ok["branches"]
        no = client.post("/api/scans/check", json={"repo_url": "https://github.com/evil/repo"}).json()
        assert no["allowed"] is False and no["provider"] == "github" and "EXEC_ALLOWED_REPOS" in no["detail"]
        assert client.post("/api/scans/check", json={"repo_url": "--upload-pack=x"}).json()["allowed"] is False


# --- a full scan through the worker --------------------------------------------------------------------------------


@pytest.fixture
def scanned(fresh):
    """A finished scan that was started from the API (so it has a request with stages)."""
    with client_for(fresh, **allowed(fresh)) as client:
        accepted = start(client, fresh)
        fresh.work()
        yield fresh, client, accepted["scan_request_id"]


def test_stages_run_in_order_and_skipped_ones_say_why(scanned):
    env, client, request_id = scanned
    detail = client.get(f"/api/scan-requests/{request_id}").json()
    assert detail["status"] == "complete" and detail["error"] is None and detail["scan_id"]
    by_stage = {s["stage"]: s for s in detail["stages"]}
    assert [s["stage"] for s in detail["stages"]] == STAGES
    assert by_stage["clone"]["status"] == "done" and "commit" in by_stage["clone"]["summary"]
    assert by_stage["layer1"]["status"] == "done" and "imports traced" in by_stage["layer1"]["summary"]
    assert by_stage["sbom"]["status"] == "done" and "159 packages" in by_stage["sbom"]["summary"]
    assert by_stage["internal"]["status"] == "skipped" and by_stage["layer2"]["status"] == "skipped"
    assert "symbol index" in by_stage["layer2"]["summary"]
    assert by_stage["vulnerabilities"]["status"] == "skipped" and "switched off" in by_stage["vulnerabilities"]["summary"]
    assert by_stage["versions"]["status"] == "done" and by_stage["changes"]["status"] == "done"
    assert by_stage["impact"]["status"] == "done" and "1 upgrade analysed, 1 waiting for a decision" in by_stage["impact"]["summary"]
    assert detail["steps_done"] == detail["steps_total"] == 9
    assert detail["found"]["packages"] == 159 and detail["found"]["declared"] == 3 and detail["found"]["imports"] > 0


def test_events_stream_replays_and_ends_with_the_request(scanned):
    env, client, request_id = scanned
    events = read_sse(client, f"/api/scan-requests/{request_id}/events")
    names = [e["event"] for e in events]
    assert names[-1] == "end" and json.loads(events[-1]["data"]) == {"status": "complete"}
    seen = [json.loads(e["data"])["stage"] for e in events if e["event"] == "scan_event"]
    assert seen[0] == "clone" and "impact" in seen
    last = events[-2]["id"]
    again = read_sse(client, f"/api/scan-requests/{request_id}/events", headers={"Last-Event-ID": last})
    assert [e["event"] for e in again] == ["end"]  # resumed after the last event: nothing is replayed


def test_summary_sbom_and_empty_vulnerability_list(scanned):
    env, client, request_id = scanned
    scan_id = client.get(f"/api/scan-requests/{request_id}").json()["scan_id"]
    summary = client.get(f"/api/scans/{scan_id}/summary").json()
    assert summary["packages"] == 159 and summary["declared"] == 3 and summary["needs_decision"] == 1
    assert summary["has_sbom"] is True and summary["scan_request_id"] == request_id
    assert summary["advisories"] == 0 and summary["repo"]
    sbom = client.get(f"/api/scans/{scan_id}/sbom")
    assert sbom.status_code == 200 and "attachment" in sbom.headers["content-disposition"]
    assert sbom.json()["specVersion"] == "1.6" and len(sbom.json()["components"]) >= 150
    listed = client.get(f"/api/scans/{scan_id}/vulnerabilities").json()
    assert listed["packages"] == 159 and listed["rows"] == [] and listed["counts"]["all"] == 0
    assert client.get("/api/scans/latest/summary").json()["scan_id"] == scan_id
    assert client.get("/api/scans/nope/summary").status_code == 404


def test_vulnerability_rows_counts_and_the_cross_repository_table(scanned):
    env, client, request_id = scanned
    scan_id = client.get(f"/api/scan-requests/{request_id}").json()["scan_id"]
    conn = core_db.connect(env.db)

    def row(vuln_id, name, severity, verdict, scope="runtime", direct=False):
        return {"vuln_id": vuln_id, "name": name, "version": "1.0.0", "purl": f"pkg:npm/{name}@1.0.0", "scope": scope,
                "direct": direct, "path": ["express", name], "severity": severity, "summary": f"{name} problem",
                "aliases": ["CVE-1"], "fixed_in": "2.0.0", "verdict": verdict, "reason": "because", "sources": ["osv"],
                "url": "https://osv.dev/x"}

    scan_store.replace_scan_vulns(conn, scan_id, [
        row("A-1", "qs", "high", "applies"), row("A-2", "debug", "medium", "does_not_apply"),
        row("A-3", "form-data", "medium", "not_shipped", scope="dev"), row("A-4", "express", "low", "undetermined", direct=True)])
    data = client.get(f"/api/scans/{scan_id}/vulnerabilities").json()
    assert data["counts"] == {"all": 4, "applies": 1, "runtime": 3, "development": 1, "transitive": 3}
    assert [r["vuln_id"] for r in data["rows"]] == ["A-1", "A-2", "A-3", "A-4"]  # severity, then verdict
    assert data["rows"][0]["path"] == ["express", "qs"] and data["rows"][0]["aliases"] == ["CVE-1"]
    everywhere = client.get("/api/vulnerabilities").json()
    assert everywhere["counts"]["all"] == 4 and everywhere["rows"][0]["repo"] and everywhere["rows"][0]["scan_id"] == scan_id
    assert client.get(f"/api/scans/{scan_id}/summary").json()["advisories_apply"] == 1


# --- scope and cancel ------------------------------------------------------------------------------------------------


def test_switched_off_stages_are_skipped_and_fixes_off_makes_the_run_report_only(tmp_path, monkeypatch):
    from tests.api.conftest import Env

    fresh = Env(tmp_path, monkeypatch)  # no earlier scan: the run is created by this one
    scope = {"vulnerabilities": False, "breaking_changes": True, "internal_contracts": False,
             "production_evidence": False, "fixes": False, "sbom": False}
    with client_for(fresh, **allowed(fresh)) as client:
        request_id = start(client, fresh, scope=scope)["scan_request_id"]
        fresh.work()
        by_stage = stages(client, request_id)
        for name in ("sbom", "internal", "vulnerabilities"):
            assert by_stage[name]["status"] == "skipped" and "Not selected" in by_stage[name]["summary"]
        scan_id = client.get(f"/api/scan-requests/{request_id}").json()["scan_id"]
        assert not os.path.exists(os.path.join(os.environ["SBOM_DIR"], f"{scan_id}.cdx.json"))
        run = client.get("/api/upgrades").json()
        express = next(r for r in run if r["package"] == "express" and r["status"] == "report_only")
        detail = client.get(f"/api/upgrades/{express['id']}").json()
        assert detail["evidence"]["production_lines"] == 0  # production evidence was not requested
        events = [e["summary"] or "" for e in
                  core_db.connect(fresh.db).execute("SELECT summary FROM run_events WHERE node = 'plan'")]
        assert any("tested fixes were not requested" in e for e in events)


def test_cancelled_request_stops_before_anything_runs(fresh):
    with client_for(fresh, **allowed(fresh)) as client:
        request_id = start(client, fresh)["scan_request_id"]
        assert client.post(f"/api/scan-requests/{request_id}/cancel").json()["status"] == "cancelled"
        before = core_db.connect(fresh.db).execute("SELECT count(*) FROM scans").fetchone()[0]
        fresh.work()
        after = core_db.connect(fresh.db).execute("SELECT count(*) FROM scans").fetchone()[0]
        assert after == before  # the scan job saw the cancellation and cloned nothing
        detail = client.get(f"/api/scan-requests/{request_id}").json()
        assert detail["status"] == "cancelled"
        assert client.post(f"/api/scan-requests/{request_id}/cancel").json()["status"] == "cancelled"  # repeating is fine


def test_a_scan_started_without_a_request_still_works(fresh):
    """CLI, webhook and schedule scans have no request: no stage rows, same result."""
    conn = core_db.connect(fresh.db)
    assert conn.execute("SELECT count(*) FROM scan_requests").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sbom_components").fetchone()[0] == 159


def test_clone_failure_is_recorded_on_the_stage(fresh):
    with client_for(fresh, **allowed(fresh)) as client:
        request_id = start(client, fresh, branch="no-such-branch")["scan_request_id"]
        fresh.work()
        detail = client.get(f"/api/scan-requests/{request_id}").json()
        assert stages(client, request_id)["clone"]["status"] == "error"
        assert "git clone failed" in stages(client, request_id)["clone"]["summary"]
        assert detail["status"] in ("running", "failed")  # failed once the retries are used up
