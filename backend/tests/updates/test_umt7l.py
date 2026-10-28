import json
import sqlite3

import pytest

from app.updates import pipeline, store
from app.updates.models import UpdateVerdict
from app.updates.registry.base import PackageInfo, VersionInfo
from app.updates.risk import RiskFacts, needs_attention, priority, recommended_target, risk_level

# --- fake registry: every package published long before the fixture scan (2026-10-26) --------


def info(name: str, versions: list[str], latest: str | None = None, **kw) -> PackageInfo:
    return PackageInfo(
        ecosystem="npm",
        name=name,
        latest_version=latest or versions[-1],
        versions=[VersionInfo(version=v, published_at="2020-01-01T00:00:00.000Z") for v in versions],
        **kw,
    )


class FakeRegistry:
    ecosystem = "npm"

    def __init__(self, packages: dict[str, PackageInfo], failing: dict[str, Exception] | None = None):
        self.packages = packages
        self.failing = failing or {}
        self.calls: list[str] = []

    async def fetch_package(self, name, *, etag=None):
        self.calls.append(name)
        if name in self.failing:
            raise self.failing[name]
        if name not in self.packages:
            return PackageInfo(ecosystem="npm", name=name, status="not_found")
        return self.packages[name]


FIXTURE_REGISTRY = {
    "supertest": info("supertest", ["6.3.0", "6.3.4", "7.0.0"]),  # ^6.3.0 -> major update, dev
    "morgan": info("morgan", ["1.10.0", "1.12.0"]),  # exact pin 1.10.0 -> minor update
    # eslint ^8.0.0 resolves to 8.5.0 and is up to date
    "eslint": info("eslint", ["8.0.0", "8.5.0"]),
}


def run(db_path, registry=None, **kw):
    return pipeline.run_dependency_updates(
        str(db_path), registry=registry or FakeRegistry(FIXTURE_REGISTRY), **kw
    )


def rows_by_name(db_path) -> dict[str, sqlite3.Row]:
    conn = store.connect(str(db_path))
    scan = store.latest_scan_id(conn)
    return {r["name"]: r for r in store.list_dependency_updates(conn, scan)}


# --- pipeline -----------------------------------------------------------------------------------


def test_every_dependency_gets_a_status(db_path):
    run_id, stats = run(db_path)
    rows = rows_by_name(db_path)
    assert set(rows) == {"supertest", "morgan", "eslint"}
    assert all(r["status"] != "pending" or r["risk_level"] for r in rows.values())
    assert stats.deps_total == 3 and sum(stats.statuses.values()) == 3
    conn = store.connect(str(db_path))
    pr = store.get_pipeline_run(conn, run_id)
    assert pr["status"] == "succeeded" and pr["finished_at"]
    assert json.loads(pr["stats"])["deps_total"] == 3


def test_row_contents(db_path):
    run(db_path)
    rows = rows_by_name(db_path)
    st = rows["supertest"]
    assert (st["current_version"], st["latest_version"], st["update_type"]) == ("6.3.4", "7.0.0", "major")
    assert st["requires_manifest_change"] == 1 and st["status"] == "pending"
    assert st["recommended_target"] == "7.0.0" and st["importer_count"] == 3
    assert st["manifest_file_id"] == "file:package.json" and st["priority_score"] > 0
    mg = rows["morgan"]
    assert (mg["current_version_basis"], mg["update_type"]) == ("exact_pin", "minor")
    es = rows["eslint"]
    assert es["status"] == "up_to_date" and es["risk_level"] == "none" and es["priority_score"] == 0


def test_dependency_type_flows_through(patched_db):
    run(patched_db)
    rows = rows_by_name(patched_db)
    assert rows["supertest"]["dependency_type"] == "dev"
    assert rows["morgan"]["dependency_type"] == "runtime"
    # same update, runtime weighs more than dev
    assert rows["morgan"]["priority_score"] > 0


def test_second_run_updates_same_rows(db_path):
    run(db_path)
    first = rows_by_name(db_path)
    registry = FakeRegistry(FIXTURE_REGISTRY)
    run(db_path, registry)
    second = rows_by_name(db_path)
    conn = store.connect(str(db_path))
    assert conn.execute("SELECT count(*) FROM dependency_updates").fetchone()[0] == 3
    assert {n: r["id"] for n, r in first.items()} == {n: r["id"] for n, r in second.items()}
    assert conn.execute("SELECT count(*) FROM pipeline_runs").fetchone()[0] == 2
    assert registry.calls == []  # registry cache hit: no network


def test_package_failure_does_not_stop_run(db_path):
    registry = FakeRegistry(FIXTURE_REGISTRY, failing={"morgan": RuntimeError("boom")})
    run_id, stats = run(db_path, registry)
    rows = rows_by_name(db_path)
    assert rows["morgan"]["status"] == "error" and "boom" in rows["morgan"]["error"]
    assert rows["supertest"]["status"] == "pending" and rows["eslint"]["status"] == "up_to_date"
    assert stats.errors == 1
    conn = store.connect(str(db_path))
    assert store.get_pipeline_run(conn, run_id)["status"] == "partial"


def test_exception_while_evaluating_one_package(db_path, monkeypatch):
    real = pipeline.resolve_versions

    def flaky(constraint, info_, scan_time, **kw):
        if info_.name == "supertest":
            raise ValueError("bad data")
        return real(constraint, info_, scan_time, **kw)

    monkeypatch.setattr(pipeline, "resolve_versions", flaky)
    run_id, stats = run(db_path)
    rows = rows_by_name(db_path)
    assert rows["supertest"]["status"] == "error" and "bad data" in rows["supertest"]["error"]
    assert rows["morgan"]["status"] == "pending"
    conn = store.connect(str(db_path))
    assert store.get_pipeline_run(conn, run_id)["status"] == "partial"


def test_not_in_registry(db_path):
    registry = FakeRegistry({k: v for k, v in FIXTURE_REGISTRY.items() if k != "eslint"})
    _, stats = run(db_path, registry)
    assert rows_by_name(db_path)["eslint"]["status"] == "not_in_registry"
    assert stats.statuses["not_in_registry"] == 1 and stats.errors == 0


def test_unsatisfiable_range_is_error_and_git_spec_is_invalid(db_path):
    conn = store.connect(str(db_path))
    conn.execute(
        "UPDATE edges SET properties = json_set(properties, '$.declared_constraint', '^9.0.0') "
        "WHERE target_id = 'dep:npm:eslint' AND type = 'DECLARES'"
    )
    conn.execute(
        "UPDATE edges SET properties = json_set(properties, '$.declared_constraint', 'github:a/b') "
        "WHERE target_id = 'dep:npm:morgan' AND type = 'DECLARES'"
    )
    conn.commit()
    run(db_path)
    rows = rows_by_name(db_path)
    assert rows["eslint"]["status"] == "error" and "no published version" in rows["eslint"]["error"]
    assert rows["morgan"]["status"] == "invalid_declaration"


def test_unsupported_ecosystem_makes_no_network_call(db_path):
    conn = store.connect(str(db_path))
    scan = store.latest_scan_id(conn)
    conn.execute(
        "INSERT INTO nodes (id, scan_id, type, name, properties) VALUES (?, ?, 'dependency', 'requests', ?)",
        ("dep:pypi:requests", scan, json.dumps({"ecosystem": "PyPI"})),
    )
    conn.commit()
    registry = FakeRegistry(FIXTURE_REGISTRY)
    _, stats = run(db_path, registry)
    assert rows_by_name(db_path)["requests"]["status"] == "unsupported_ecosystem"
    assert "requests" not in registry.calls and stats.deps_total == 4


def test_only_filter(db_path):
    _, stats = run(db_path, only=["morgan"])
    assert set(rows_by_name(db_path)) == {"morgan"} and stats.deps_total == 1


def test_stats_counts(db_path):
    _, stats = run(db_path)
    assert stats.updates == {"patch": 0, "minor": 1, "major": 1}
    assert stats.up_to_date == 1 and stats.undeclared_imports == 1
    assert stats.llm_calls == 0 and stats.cache_hits == 0


def test_run_fails_cleanly_without_dependencies(tmp_path):
    from .conftest import ORIGINAL_DDL

    p = tmp_path / "empty.db"
    c = sqlite3.connect(p)
    c.executescript(ORIGINAL_DDL)
    c.commit()
    c.close()
    from app.updates.preflight import PreflightError

    with pytest.raises(PreflightError):
        run(p)


def test_cli_run(patched_db, capsys, monkeypatch):
    from app.updates import cli

    monkeypatch.setattr(pipeline, "NpmRegistry", lambda http: FakeRegistry(FIXTURE_REGISTRY))
    assert cli.main(["run", "--db", str(patched_db), "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "succeeded" in out and "supertest" in out and "Top 2 by priority" in out


# --- risk rules -----------------------------------------------------------------------------


def facts(**kw) -> RiskFacts:
    return RiskFacts(**kw)


@pytest.mark.parametrize(
    "kw,expected",
    [
        (dict(vuln_severities=["critical"], dependency_type="dev"), "critical"),
        (dict(vuln_severities=["high"], dependency_type="runtime"), "critical"),
        (dict(vuln_severities=["high"], dependency_type="unknown"), "critical"),
        (dict(vuln_severities=["high"], dependency_type="dev"), "high"),
        (dict(has_breaking=True, importer_count=2, dependency_type="runtime", update_type="major"), "high"),
        (dict(is_deprecated=True, latest_deprecated=True, update_type="none"), "high"),
        (dict(has_breaking=True, importer_count=0, dependency_type="runtime", update_type="major"), "medium"),
        (dict(has_breaking=True, importer_count=5, dependency_type="dev", update_type="major"), "medium"),
        (dict(runtime_requirement_change=True, update_type="major"), "medium"),
        (dict(vuln_severities=["medium"], dependency_type="dev"), "medium"),
        (dict(archived=True, update_type="minor"), "medium"),
        (dict(update_type="minor"), "low"),
        (dict(update_type="none", notes_unavailable=True), "low"),
        (dict(is_deprecated=True, latest_deprecated=False, update_type="major"), "low"),
        (dict(vuln_severities=["low"], update_type="none"), "low"),
        (dict(update_type="none"), "none"),
    ],
)
def test_risk_level_rules(kw, expected):
    assert risk_level(facts(**kw)) == expected


def test_risk_first_match_wins():
    # critical beats everything else that is also true
    f = facts(vuln_severities=["critical", "medium"], has_breaking=True, importer_count=3,
              dependency_type="runtime", is_deprecated=True, latest_deprecated=True)
    assert risk_level(f) == "critical"


def test_priority_formula():
    f = facts(update_type="major", dependency_type="runtime", importer_count=3)
    assert priority(f) == 30 + 10  # 5 * log2(4) = 10
    f = facts(update_type="major", dependency_type="dev", importer_count=3)
    assert priority(f) == 20.0  # dev weight 0.5
    f = facts(update_type="minor", dependency_type="unknown", is_deprecated=True,
              runtime_requirement_change=True, vuln_severities=["high"], importer_count=0)
    assert priority(f) == round((80 + 40 + 10 + 15) * 0.8, 1)


def test_priority_importer_bonus_capped_and_zero_when_up_to_date():
    assert priority(facts(update_type="patch", dependency_type="runtime", importer_count=10**6)) == 3 + 20
    assert priority(facts(update_type="none", importer_count=80)) == 0
    assert not needs_attention(facts(update_type="none"))
    assert needs_attention(facts(update_type="none", is_deprecated=True))


def test_recommended_target():
    v = UpdateVerdict(current_version="1.2.0", latest_in_range="1.4.0", latest_version="2.0.0")
    assert recommended_target(v) == "1.4.0"
    assert recommended_target(v, "1.3.0", True) == "1.3.0"
    assert recommended_target(v, "1.9.0", False) == "2.0.0"
    assert recommended_target(v, "1.0.0", True) == "1.4.0"  # already past the fix
    up_to_date = UpdateVerdict(current_version="2.1.0", latest_in_range="2.1.0", latest_version="1.3.8")
    assert recommended_target(up_to_date) == "2.1.0"  # never a downgrade
    only_major = UpdateVerdict(current_version="1.2.0", latest_in_range="1.2.0", latest_version="2.0.0")
    assert recommended_target(only_major) == "2.0.0"
