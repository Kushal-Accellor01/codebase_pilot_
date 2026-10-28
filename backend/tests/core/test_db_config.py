import sqlite3

import pytest

from app.core import db as core_db
from app.core.config import Settings, get_settings
from app.core.contracts import DependencyChangeEvent, Finding, PatchResult, Plan, ProbeResult
from tests.updates.conftest import make_db


@pytest.fixture(params=[True, False], ids=["patched", "original"])
def db(request, tmp_path):
    return make_db(tmp_path / "t.db", patched=request.param)


def test_migrations_apply_twice_and_are_recorded(db):
    conn = core_db.connect(str(db))
    assert core_db.migrate(conn) == ["002_dependency_updates", "003_pipeline"]
    assert core_db.migrate(conn) == []
    assert core_db.applied_versions(conn) == ["002_dependency_updates", "003_pipeline"]
    tables = core_db.table_names(conn)
    for t in ("dependency_updates", "jobs", "upgrade_runs", "run_events", "impact_findings", "telemetry_evidence",
              "remediation_plans", "patches", "validations", "approvals", "pull_requests", "llm_cache", "feedback"):
        assert t in tables, t
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(validations)")}
    assert {"tests_passed", "tests_total"} <= cols


def test_migrate_repairs_db_migrated_before_the_runner_existed(db):
    conn = core_db.connect(str(db))
    conn.executescript(core_db.MIGRATIONS[0].path.read_text(encoding="utf-8"))  # 002 only, no bookkeeping
    assert core_db.migrate(conn) == ["002_dependency_updates", "003_pipeline"]


def test_migrate_requires_ingestion_tables(tmp_path):
    conn = core_db.connect(str(tmp_path / "x.db"), must_exist=False)
    with pytest.raises(core_db.DatabaseError, match="Ingestion tables missing"):
        core_db.migrate(conn)


def test_job_dedupe_key_is_unique(db):
    conn = core_db.connect(str(db))
    core_db.migrate(conn)
    conn.execute("INSERT INTO jobs (id, kind, payload, dedupe_key) VALUES ('1', 'scan', '{}', 'k')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (id, kind, payload, dedupe_key) VALUES ('2', 'scan', '{}', 'k')")


def test_settings_treat_empty_values_as_unset(monkeypatch):
    for name, value in (("NPM_REGISTRY_URL", ""), ("GITHUB_TOKEN", ""), ("LLM_MODE", ""), ("BOT_PR_MODE", "")):
        monkeypatch.setenv(name, value)
    s = get_settings()
    assert s.npm_registry_url == "https://registry.npmjs.org" and s.github_token is None
    assert s.llm_mode == "live" and s.bot_pr_mode == "comment_only"


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "acme/shop, acme/other ,")
    monkeypatch.setenv("LLM_MODE", "replay")
    s = Settings()
    assert s.exec_allowed == {"acme/shop", "acme/other"} and s.llm_mode == "replay"


def test_contracts_validate():
    ev = DependencyChangeEvent(
        event_id="s:npm:express:4.21.2->5.1.0", scan_id="s", ecosystem="npm", package="express",
        dependency_type="runtime", manifest_path="package.json", current_version="4.21.2",
        current_version_basis="exact_pin", update_type="major", requires_manifest_change=True,
        runtime_requirement_change=True, is_deprecated=False, importer_count=4, priority_score=70.0,
        risk_level="high", status="analyzed",
    )
    assert DependencyChangeEvent.model_validate(ev.model_dump()) == ev
    Finding(file_path="a.js", line=1, snippet="x", sources=["pattern"], confidence=0.6)
    Plan(severity="high", effort_hours=2.5, rationale="r", steps=["a"], validation=["b"])
    patch = PatchResult(attempt=1, method="llm_edits", edits=[], diff="", manifest_change=None,
                        validation=ProbeResult(command="npm test", exit_code=0, passed=True, duration_ms=1,
                                               output_tail=""))
    assert patch.validation.passed
