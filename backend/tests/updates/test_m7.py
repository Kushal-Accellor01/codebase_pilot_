"""U-M7 acceptance: the whole Step 2 pipeline on a scan whose app pins Express 4.21.2."""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import Settings
from app.updates import cli, pipeline, store
from app.updates.events import build_events
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from app.updates.models import ChangeItem, UpdateAnalysisOut
from app.updates.registry.base import PackageInfo, VersionInfo
from tests.updates.conftest import make_db
from tests.updates.test_m6 import FakeLLM
from tests.updates.test_umt7l import FakeRegistry, info

FIX = Path(__file__).parent.parent / "fixtures"
GH = "https://api.github.com"
BATCH = "https://api.osv.dev/v1/querybatch"

EXPRESS_VERSIONS = ["4.21.2", "4.22.0", "4.22.1", "4.22.2", "5.0.0", "5.0.1", "5.1.0", "5.2.0", "5.2.1", "5.3.0"]


def run(coro):
    return asyncio.run(coro)


def releases() -> list:
    return json.loads((FIX / "notes" / "express_releases.json").read_text(encoding="utf-8"))


def express_db(tmp_path) -> Path:
    """The standard fixture scan plus an exact-pinned 'express' used by lib/app.js."""
    path = make_db(tmp_path / "e.db", patched=True)
    conn = store.connect(str(path))
    props = lambda d: json.dumps(d)  # noqa: E731
    conn.execute("INSERT INTO nodes (id, scan_id, type, name, properties) VALUES (?, 'scan-new', 'dependency', "
                 "'express', ?)", ("dep:npm:express", props({"ecosystem": "npm"})))
    conn.execute("INSERT INTO nodes (id, scan_id, type, name, properties) VALUES ('mod:express', 'scan-new', "
                 "'module', 'express', ?)", (props({"is_import": True}),))
    conn.execute("INSERT INTO edges VALUES ('file:package.json', 'dep:npm:express', 'DECLARES', 'scan-new', ?)",
                 (props({"declared_constraint": "4.21.2", "dependency_type": "runtime"}),))
    conn.execute("INSERT INTO edges VALUES ('mod:express', 'dep:npm:express', 'RESOLVES_TO', 'scan-new', '{}')")
    conn.execute("INSERT INTO edges VALUES ('file:lib/app.js', 'mod:express', 'IMPORTS', 'scan-new', ?)",
                 (props({"lines": [3], "specifiers": ["express"]}),))
    conn.commit()
    conn.close()
    return path


def registry() -> FakeRegistry:
    express = PackageInfo(
        ecosystem="npm", name="express", latest_version="5.3.0", repository="expressjs/express",
        versions=[VersionInfo(version=v, published_at="2020-01-01T00:00:00.000Z",
                              engines={"node": ">= 0.10" if v.startswith("4") else ">= 18"})
                  for v in EXPRESS_VERSIONS],
    )
    return FakeRegistry({
        "express": express,
        "supertest": info("supertest", ["6.3.4"]),
        "morgan": info("morgan", ["1.10.0"]),
        "eslint": info("eslint", ["8.5.0"]),
    })


def mock_services(*, notes: bool = True, repo_ok: bool = True):
    respx.post(BATCH).mock(side_effect=lambda req: httpx.Response(
        200, json={"results": [{} for _ in json.loads(req.content)["queries"]]}))
    if repo_ok:
        respx.get(f"{GH}/repos/expressjs/express/contents/package.json").respond(
            200, text=json.dumps({"name": "express"}))
        respx.get(f"{GH}/repos/expressjs/express").respond(
            200, json={"name": "express", "owner": {"login": "expressjs"}, "default_branch": "master",
                       "archived": False})
    else:
        respx.get(f"{GH}/repos/expressjs/express/contents/package.json").respond(404)
        respx.get("https://api.deps.dev/v3/systems/npm/packages/express/versions/5.3.0").respond(404)
    respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=releases() if notes else [])
    respx.get(url__regex=rf"{GH}/repos/expressjs/express/(contents/(?!package\.json).*|compare/.*)").respond(404)


def breaking_changes_from_real_notes(count: int = 6) -> list[ChangeItem]:
    """Quote real lines from the recorded Express 5.0.0 release notes."""
    body = next(r["body"] for r in releases() if r["tag_name"] == "v5.0.0")
    lines = [ln.strip("-* ").strip() for ln in body.splitlines() if len(ln.strip("-* ").strip()) > 30]
    lines = [re.sub(r"\s+", " ", ln)[:250] for ln in lines][:count]
    return [
        ChangeItem(kind="removed", breaking=True, api_symbols=[], description=f"Change {i}", version="5.0.0",
                   evidence_quote=quote)
        for i, quote in enumerate(lines)
    ]


def scripted_llm(conn, changes):
    return FakeLLM(conn, [UpdateAnalysisOut(summary="Express 5 removes several APIs.", changes=changes,
                                            notes_quality="detailed")])


async def run_full(db_path, tmp_path, llm_factory=None, **kw):
    async with HttpClient(wait_scale=0) as http:
        conn = store.connect(str(db_path))
        llm = llm_factory(conn) if llm_factory else None
        try:
            return await pipeline.run_dependency_updates_async(
                str(db_path), registry=registry(), http=http, github=GitHubClient(http, "t"), llm=llm,
                reports_dir=tmp_path / "reports", **kw,
            )
        finally:
            conn.close()


@respx.mock
def test_full_pipeline_express_event_with_validated_breaking_changes(tmp_path):
    mock_services()
    db = express_db(tmp_path)
    changes = breaking_changes_from_real_notes(6)
    assert len(changes) == 6
    run_id, stats = run(run_full(db, tmp_path, lambda c: scripted_llm(c, changes)))

    conn = store.connect(str(db))
    scan = store.latest_scan_id(conn)
    rows = {r["name"]: r for r in store.list_dependency_updates(conn, scan)}
    ex = rows["express"]
    assert (ex["current_version"], ex["latest_version"], ex["update_type"]) == ("4.21.2", "5.3.0", "major")
    assert ex["status"] == "analyzed" and ex["analysis_id"] and ex["recommended_target"] == "5.3.0"
    assert ex["risk_level"] == "high"  # breaking change + importer + runtime dependency
    assert store.get_pipeline_run(conn, run_id)["status"] == "succeeded"
    assert stats.analysed == 1 and stats.llm_calls == 1 and stats.errors == 0

    # >= 5 validated breaking changes; every quote really is in the stored release notes
    stored = store.load_changes(conn, ex["analysis_id"])
    assert len(stored) >= 5 and all(c["breaking"] for c in stored)
    bodies = " ".join(
        re.sub(r"\s+", " ", r["body_md"].replace("`", "")).casefold()
        for r in conn.execute("SELECT body_md FROM release_notes WHERE name = 'express'")
    )
    for c in stored:
        assert re.sub(r"\s+", " ", c["evidence_quote"].replace("`", "")).casefold().strip(" .*-") in bodies
        assert c["evidence_url"].startswith("https://github.com/expressjs/express/")

    # event for impact analysis
    events = {e.package: e for e in build_events(conn, scan)}
    event = events["express"]
    assert event.event_id == f"{scan}:npm:express:4.21.2->5.3.0"
    assert len(event.changes) >= 5 and event.summary == "Express 5 removes several APIs."
    assert event.source_repo == "github.com/expressjs/express" and event.manifest_path == "package.json"
    assert [f.path for f in event.importing_files] == ["lib/app.js"] and event.importing_files[0].lines == [3]
    assert event.runtime_requirement_change and event.notes_coverage and event.notes_coverage > 0.5

    # reports written (S2 5.11)
    json_path = tmp_path / "reports" / f"dependency_updates_{scan}.json"
    md_path = tmp_path / "reports" / f"dependency_updates_{scan}.md"
    assert any(e["package"] == "express" for e in json.loads(json_path.read_text(encoding="utf-8")))
    md = md_path.read_text(encoding="utf-8")
    assert md.startswith("# Dependency updates:") and "| high | express | runtime | 4.21.2 → 5.3.0 | major |" in md


@respx.mock
def test_second_run_makes_zero_llm_calls_and_no_duplicate_rows(tmp_path):
    mock_services()
    db = express_db(tmp_path)
    changes = breaking_changes_from_real_notes(6)
    run(run_full(db, tmp_path, lambda c: scripted_llm(c, changes)))

    def second(conn):
        return FakeLLM(conn, [])  # any LLM call would pop from an empty list and fail

    _, stats = run(run_full(db, tmp_path, second))
    assert stats.llm_calls == 0 and stats.cache_hits == 1 and stats.analysed == 1 and stats.errors == 0
    conn = store.connect(str(db))
    assert conn.execute("SELECT count(*) FROM dependency_updates").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM update_analyses").fetchone()[0] == 1
    row = conn.execute("SELECT analysis_id FROM dependency_updates WHERE name = 'express'").fetchone()
    assert row["analysis_id"] is not None  # the link survives a re-run


@respx.mock
def test_invented_quote_never_reaches_the_event(tmp_path):
    mock_services()
    db = express_db(tmp_path)
    changes = breaking_changes_from_real_notes(5)
    changes.append(ChangeItem(kind="removed", breaking=True, api_symbols=["app.magic"], description="invented",
                              version="5.0.0", evidence_quote="Express 5 removes app.magic() entirely"))
    run(run_full(db, tmp_path, lambda c: scripted_llm(c, changes)))
    conn = store.connect(str(db))
    scan = store.latest_scan_id(conn)
    event = {e.package: e for e in build_events(conn, scan)}["express"]
    assert len(event.changes) == 5 and all("magic" not in c.evidence_quote for c in event.changes)
    dropped = json.loads(conn.execute("SELECT dropped_json FROM update_analyses").fetchone()[0])
    assert dropped[0]["evidence_quote"].startswith("Express 5 removes app.magic")


@respx.mock
def test_source_unknown_when_repository_does_not_verify(tmp_path):
    mock_services(repo_ok=False)
    db = express_db(tmp_path)
    run(run_full(db, tmp_path, lambda c: FakeLLM(c, [])))
    conn = store.connect(str(db))
    scan = store.latest_scan_id(conn)
    ex = {r["name"]: r for r in store.list_dependency_updates(conn, scan)}["express"]
    assert ex["status"] == "source_unknown" and ex["analysis_id"] is None
    events = {e.package: e for e in build_events(conn, scan)}
    assert "express" in events  # a person has to help, so it is emitted
    assert events["express"].status == "source_unknown" and events["express"].changes == []


@respx.mock
def test_notes_unavailable_makes_no_llm_call(tmp_path):
    mock_services(notes=False)
    db = express_db(tmp_path)
    llm_holder = {}

    def factory(conn):
        llm_holder["llm"] = FakeLLM(conn, [])
        return llm_holder["llm"]

    _, stats = run(run_full(db, tmp_path, factory))
    conn = store.connect(str(db))
    ex = {r["name"]: r for r in store.list_dependency_updates(conn, store.latest_scan_id(conn))}["express"]
    assert ex["status"] == "notes_unavailable" and ex["risk_level"] in ("low", "medium")
    assert llm_holder["llm"].calls == 0 and stats.notes_unavailable == 1


@respx.mock
def test_llm_failure_keeps_deterministic_results_and_marks_run_partial(tmp_path):
    from app.core.llm import LLMOutputError

    mock_services()
    db = express_db(tmp_path)

    class Failing(FakeLLM):
        async def chat(self, *a, **k):
            raise LLMOutputError("bad json twice")

    _, stats = run(run_full(db, tmp_path, lambda c: Failing(c, [])))
    conn = store.connect(str(db))
    scan = store.latest_scan_id(conn)
    ex = {r["name"]: r for r in store.list_dependency_updates(conn, scan)}["express"]
    assert ex["status"] == "error" and ex["error"].startswith("analysis_failed")
    assert ex["current_version"] == "4.21.2" and ex["latest_version"] == "5.3.0"  # facts survive
    assert store.get_pipeline_run(conn, stats and conn.execute(
        "SELECT id FROM pipeline_runs ORDER BY started_at DESC LIMIT 1").fetchone()["id"])["status"] == "partial"


@respx.mock
def test_llm_not_configured_is_a_warning_not_a_failure(tmp_path):
    mock_services()
    db = express_db(tmp_path)

    async def go():
        async with HttpClient(wait_scale=0) as http:
            from app.core.llm import LLM

            llm = LLM(store.connect(str(db)), http, Settings(_env_file=None))
            return await pipeline.run_dependency_updates_async(
                str(db), registry=registry(), http=http, github=GitHubClient(http, "t"), llm=llm,
                reports_dir=tmp_path / "reports")

    _, stats = run(go())
    assert any("LLM analysis skipped" in w for w in stats.warnings)
    conn = store.connect(str(db))
    ex = {r["name"]: r for r in store.list_dependency_updates(conn, store.latest_scan_id(conn))}["express"]
    assert ex["status"] == "pending"  # notes were fetched, nothing interpreted


@respx.mock
def test_github_rate_limit_leaves_packages_pending_and_run_partial(tmp_path):
    respx.post(BATCH).mock(side_effect=lambda req: httpx.Response(
        200, json={"results": [{} for _ in json.loads(req.content)["queries"]]}))
    respx.get(f"{GH}/repos/expressjs/express/contents/package.json").respond(
        200, text=json.dumps({"name": "express"}),
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"})
    respx.get(f"{GH}/repos/expressjs/express").respond(200, json={"name": "express", "owner": {"login": "expressjs"}})
    db = express_db(tmp_path)
    _, stats = run(run_full(db, tmp_path, lambda c: FakeLLM(c, [])))
    assert any("rate limit" in w for w in stats.warnings)
    conn = store.connect(str(db))
    ex = {r["name"]: r for r in store.list_dependency_updates(conn, store.latest_scan_id(conn))}["express"]
    assert ex["status"] == "pending"


def test_no_github_traffic_without_notes_stage(tmp_path):
    db = express_db(tmp_path)  # no respx: any network call would fail the run
    _, stats = run(run_full(db, tmp_path, security=False, notes=False))
    assert stats.deps_total == 4 and stats.llm_calls == 0


@respx.mock
def test_cli_report_and_run_writes_files(tmp_path, capsys, monkeypatch):
    mock_services()
    db = express_db(tmp_path)
    run(run_full(db, tmp_path, lambda c: scripted_llm(c, breaking_changes_from_real_notes(5))))
    assert cli.main(["report", "--db", str(db), "--format", "md"]) == 0
    md = capsys.readouterr().out
    assert "Totals: 4 dependencies" in md and "express" in md
    assert cli.main(["report", "--db", str(db), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(e["package"] == "express" and len(e["changes"]) == 5 for e in data)


def test_events_only_for_risk_medium_or_when_a_person_must_help(tmp_path):
    db = express_db(tmp_path)
    run(run_full(db, tmp_path, security=False, notes=False))
    conn = store.connect(str(db))
    scan = store.latest_scan_id(conn)
    assert {e.package for e in build_events(conn, scan)} <= {"express", "supertest", "morgan", "eslint"}
    low_only = [e for e in build_events(conn, scan) if e.risk_level in ("low", "none")]
    assert all(e.status in ("source_unknown", "notes_unavailable") for e in low_only)
    assert len(build_events(conn, scan, only_actionable=False)) == 4


def test_markdown_report_shape(tmp_path):
    db = express_db(tmp_path)
    run(run_full(db, tmp_path, security=False, notes=False))
    from app.updates.report import render_markdown

    conn = store.connect(str(db))
    md = render_markdown(conn, store.latest_scan_id(conn))
    first, second = md.splitlines()[:2]
    assert first.startswith("# Dependency updates:") and "(scan scan-new" in first
    assert second.startswith("Totals: 4 dependencies") and "up to date" in second
    assert "| Risk | Package | Type | Current → Target | Update | Vulns | Breaking changes | Used in |" in md


@pytest.mark.parametrize("flag", ["--skip-notes", "--no-llm"])
def test_cli_run_flags_parse(flag, tmp_path, monkeypatch):
    seen = {}

    def fake(db, scan, **kw):
        seen.update(kw)
        raise SystemExit(0)

    monkeypatch.setattr("app.updates.pipeline.run_dependency_updates", fake)
    with pytest.raises(SystemExit):
        cli.main(["run", "--db", str(tmp_path / "x.db"), flag])
    assert seen["notes"] is (flag != "--skip-notes") and seen["analysis"] is (flag != "--no-llm")
