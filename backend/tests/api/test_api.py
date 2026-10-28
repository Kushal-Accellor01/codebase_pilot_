import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core import db as core_db
from app.core import jobs
from app.core.config import Settings
from app.orchestrator import store as run_store
from app.updates import sources

pytestmark = [pytest.mark.node]


def client_for(env, **settings) -> TestClient:
    return TestClient(create_app(env.db, Settings(_env_file=None, codebase_pilot_db=env.db, **settings)))


@pytest.fixture
def client(seeded):
    with client_for(seeded) as c:
        yield c


def express_run_id(client) -> str:
    runs = client.get("/api/upgrades").json()
    return next(r["id"] for r in runs if r["package"] == "express")


# --- reads -------------------------------------------------------------------------------------------------


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and "003_pipeline" in body["migrations"] and body["llm_mode"] == "live"


def test_scans(client):
    scans = client.get("/api/scans").json()
    scan = next(s for s in scans if s["has_nodes"])
    assert scan["repo"] == "pilot-sandbox-shop" and scan["dependencies"] >= 3 and scan["critical"] >= 1
    assert scan["commit_sha"] and len(scan["commit_sha"]) == 40


def test_dependencies(client):
    rows = {r["name"]: r for r in client.get("/api/scans/latest/dependencies").json()}
    express = rows["express"]
    assert (express["current_version"], express["update_type"], express["risk_level"]) == (
        "4.21.2",
        "major",
        "critical",
    )
    assert express["reachability"] == "reachable_in_code" and express["run_status"] == "awaiting_approval"
    assert express["importer_count"] >= 3 and express["dependency_type"] in ("runtime", "unknown")
    assert set(rows) == {"express"}  # the pipeline ran with --only express


def test_overview_headline_board_and_files(client):
    ov = client.get("/api/scans/latest/overview").json()
    assert ov["headline"].startswith("1 upgrade needs a decision. Express 5 would break ")
    assert "already warn" in ov["headline"] and ov["lede"].startswith("Pilot checked 1 dependency against")
    top = ov["upgrades"][0]
    assert top["package"] == "express" and top["risk"] == "critical" and top["from"] == "4.21.2"
    assert top["status_label"] == "Awaiting approval" and top["pill"] == "wait"
    assert top["why_bold"] in top["why"] and "already warn" in top["why"]
    assert [w["package"] for w in ov["waiting"]] == ["express"] and "Fix tested" in ov["waiting"][0]["detail"]
    assert ov["approvals_waiting"] == 1 and sum(ov["risk"].values()) == ov["total"] == 1 and ov["risk"]["critical"] == 1
    files = {f["path"]: f for f in ov["affected_files"]}
    assert "routes/orders.js" in files and files["routes/orders.js"]["strongest_evidence"] == "Failing test"
    assert "lib/notify.js" not in files


def test_dependency_update_event(client):
    rows = {r["name"]: r for r in client.get("/api/scans/latest/dependencies").json()}
    event = client.get(f"/api/dependency-updates/{rows['express']['id']}").json()
    assert event["package"] == "express" and len(event["changes"]) >= 5 and event["run_status"] == "awaiting_approval"
    assert event["source_repo"] == "github.com/expressjs/express"
    assert all(c["evidence_url"].startswith("https://github.com/expressjs/express/") for c in event["changes"])


def test_upgrade_list_filter_and_detail(client):
    assert client.get("/api/upgrades?status=awaiting_approval").json()[0]["package"] == "express"
    assert client.get("/api/upgrades?status=pr_opened").json() == []
    detail = client.get(f"/api/upgrades/{express_run_id(client)}").json()
    assert detail["can_decide"] is True and detail["needs_engineer"] is False
    assert detail["run"]["to"] == "5.3.0" and detail["plan"]["severity"] == "critical"
    findings = {(f["file_path"], f["line"]): f for f in detail["findings"]}
    assert findings[("routes/orders.js", 8)]["owners"] == ["@acme/team-shop"]
    assert set(findings[("routes/orders.js", 8)]["sources"]) >= {"pattern", "runtime_deprecation", "test_failure"}
    assert not any(f["file_path"] == "lib/notify.js" for f in detail["findings"])
    tests = detail["tests"]
    assert tests["current"]["passed"] is True and tests["unpatched"]["passed"] is False
    assert tests["patched"]["passed"] == 1 and tests["patched"]["tests_passed"] == 9
    assert detail["evidence"]["runtime_warnings"] >= 5 and detail["evidence"]["production_lines"] > 5
    assert detail["patches"][-1]["diff"].startswith("diff --git") and detail["pull_request"] is None


def test_blast_radius_graph_marks_notify_as_not_affected(client):
    run_id = express_run_id(client)
    graph = client.get(f"/api/scans/latest/graph?run_id={run_id}").json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["dep:express"]["status"] == "target"
    assert nodes["file:routes/orders.js"]["status"] == "affected" and nodes["file:routes/orders.js"]["findings"] >= 2
    assert nodes["file:lib/notify.js"]["status"] == "blast_radius" and nodes["file:lib/notify.js"]["findings"] == 0
    assert nodes["team:@acme/team-shop"]["kind"] == "team"
    kinds = {e["kind"] for e in graph["edges"]}
    assert {"imports", "resolves_to", "owns"} <= kinds
    ids = set(nodes)
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    assert {"source": "file:routes/orders.js", "target": "file:lib/notify.js", "kind": "imports"} in graph["edges"]


def test_not_found(client):
    assert client.get("/api/upgrades/nope").status_code == 404
    assert client.get("/api/dependency-updates/99999").status_code == 404
    assert client.get("/api/scans/nope/dependencies").status_code == 404
    assert client.get("/api/scans/latest/graph?run_id=nope").status_code == 404
    assert client.get("/api/upgrades/nope/events").status_code == 404


def test_openapi_describes_the_types_the_frontend_needs(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("Overview", "BoardItem", "DependencyRow", "UpgradeDetail", "Graph", "DecisionRequest"):
        assert name in schemas
    assert "from" in schemas["BoardItem"]["properties"]


# --- SSE ----------------------------------------------------------------------------------------------------------------------


def read_sse(client, url, headers=None):
    events, current = [], {}
    with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith(":"):
                continue
            if not line:
                if current:
                    events.append(current)
                    if current.get("event") == "end":
                        break
                    current = {}
                continue
            key, _, value = line.partition(": ")
            current[key] = value
    return events


def test_sse_replays_then_ends_at_the_approval_pause(client):
    run_id = express_run_id(client)
    events = read_sse(client, f"/api/upgrades/{run_id}/events")
    run_events = [e for e in events if e.get("event") == "run_event"]
    assert [json.loads(e["data"])["node"] for e in run_events][:2] == ["prepare", "prepare"]
    assert json.loads(run_events[-1]["data"])["status"] == "waiting"
    assert events[-1]["event"] == "end" and json.loads(events[-1]["data"])["status"] == "awaiting_approval"
    ids = [int(e["id"]) for e in run_events]
    assert ids == sorted(ids)


def test_sse_resumes_after_last_event_id(client):
    run_id = express_run_id(client)
    first = [e for e in read_sse(client, f"/api/upgrades/{run_id}/events") if e.get("event") == "run_event"]
    cut = int(first[3]["id"])
    rest = [
        e
        for e in read_sse(client, f"/api/upgrades/{run_id}/events", {"Last-Event-ID": str(cut)})
        if e.get("event") == "run_event"
    ]
    assert [e["id"] for e in rest] == [e["id"] for e in first[4:]]
    again = [e for e in read_sse(client, f"/api/upgrades/{run_id}/events?after={cut}") if e.get("event") == "run_event"]
    assert [e["id"] for e in again] == [e["id"] for e in rest]


# --- writes ----------------------------------------------------------------------------------------------------------------------


def test_feedback_is_stored_and_validated(client, seeded):
    detail = client.get(f"/api/upgrades/{express_run_id(client)}").json()
    conn = core_db.connect(seeded.db)
    finding_id = conn.execute("SELECT id FROM impact_findings LIMIT 1").fetchone()["id"]
    r = client.post(
        "/api/feedback",
        json={
            "target_kind": "impact_finding",
            "target_id": finding_id,
            "verdict": "not_relevant",
            "comment": "test helper",
        },
    )
    assert r.status_code == 201
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (r.json()["id"],)).fetchone()
    assert (row["verdict"], row["user_name"], row["comment"]) == ("not_relevant", "demo", "test helper")
    assert (
        client.post(
            "/api/feedback", json={"target_kind": "impact_finding", "target_id": 99999, "verdict": "correct"}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/feedback", json={"target_kind": "nonsense", "target_id": 1, "verdict": "correct"}).status_code
        == 422
    )
    assert detail["run"]["package"] == "express"


def test_source_override_writes_yaml_and_forgets_the_cached_source(client, seeded):
    conn = core_db.connect(seeded.db)
    store_rows = conn.execute("SELECT count(*) FROM package_sources WHERE name = 'express'").fetchone()[0]
    assert store_rows == 1
    r = client.post("/api/sources/override", json={"name": "express", "owner": "expressjs", "repo": "express"})
    assert r.status_code == 201
    assert sources.load_overrides(sources.overrides_path())["npm/express"] == {"owner": "expressjs", "repo": "express"}
    assert conn.execute("SELECT count(*) FROM package_sources WHERE name = 'express'").fetchone()[0] == 0
    assert client.post("/api/sources/override", json={"name": "", "owner": "a", "repo": "b"}).status_code == 422


def test_scan_request_queues_a_job(client, seeded):
    r = client.post("/api/scans", json={"repo_url": "https://github.com/acme/shop", "only": ["express"]})
    assert r.status_code == 202
    conn = core_db.connect(seeded.db)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (r.json()["job_id"],)).fetchone()
    assert job["kind"] == "scan" and json.loads(job["payload"])["repo_url"] == "https://github.com/acme/shop"
    assert client.post("/api/scans", json={"repo_url": ""}).status_code == 422
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (r.json()["job_id"],))


def test_decision_flow_approve_then_conflict(fresh):
    with client_for(fresh) as client:
        run_id = express_run_id(client)
        assert (
            client.post(f"/api/upgrades/{run_id}/decision", json={"decision": "maybe", "decided_by": "a"}).status_code
            == 422
        )
        assert (
            client.post("/api/upgrades/nope/decision", json={"decision": "approved", "decided_by": "a"}).status_code
            == 404
        )
        r = client.post(
            f"/api/upgrades/{run_id}/decision", json={"decision": "approved", "decided_by": "alice", "comment": "ok"}
        )
        assert r.status_code == 202 and r.json() == {"status": "queued"}
        # a second, conflicting decision while the first is queued
        assert (
            client.post(
                f"/api/upgrades/{run_id}/decision", json={"decision": "rejected", "decided_by": "bob"}
            ).status_code
            == 409
        )
        fresh.work()  # the worker picks the job up
        detail = client.get(f"/api/upgrades/{run_id}").json()
        assert detail["run"]["status"] == "pr_opened" and detail["can_decide"] is False
        assert detail["approval"]["decided_by"] == "alice" and detail["pull_request"]["state"] == "local"
        assert (
            client.post(
                f"/api/upgrades/{run_id}/decision", json={"decision": "approved", "decided_by": "x"}
            ).status_code
            == 409
        )
        ov = client.get("/api/scans/latest/overview").json()
        assert ov["waiting"] == [] and ov["upgrades"][0]["status_label"] == "Pull request opened"
        assert ov["headline"].startswith("Everything") or "decision" not in ov["headline"]


def test_embedded_worker_processes_a_decision_without_a_separate_process(fresh):
    import time

    from app.orchestrator.worker import Services

    async_services = Services(registry=None)  # the decision path needs no registry
    with TestClient(
        create_app(
            fresh.db, Settings(_env_file=None, codebase_pilot_db=fresh.db, pilot_embedded_worker=True), async_services
        )
    ) as client:
        assert client.get("/api/health").json()["embedded_worker"] is True
        run_id = express_run_id(client)
        client.post(f"/api/upgrades/{run_id}/decision", json={"decision": "rejected", "decided_by": "alice"})
        deadline = time.time() + 30
        while time.time() < deadline:
            if client.get(f"/api/upgrades/{run_id}").json()["run"]["status"] == "rejected":
                break
            time.sleep(0.3)
        assert client.get(f"/api/upgrades/{run_id}").json()["run"]["status"] == "rejected"
        assert run_store.load_approval(core_db.connect(fresh.db), run_id)["decision"] == "rejected"
    assert jobs.counts(core_db.connect(fresh.db)).get("failed", 0) == 0
