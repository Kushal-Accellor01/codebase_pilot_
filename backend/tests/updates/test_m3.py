import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.updates import pipeline, store
from app.updates.http import HttpClient
from app.updates.models import VulnRecord
from app.updates.security import (
    PackageVuln,
    check_security,
    cvss3_base_score,
    fixed_in,
    min_safe_version,
    parse_vuln,
    query_batch,
    severity_from_score,
    severity_of,
)

from .test_umt7l import FakeRegistry, info, rows_by_name

OSV = Path(__file__).parent.parent / "fixtures" / "osv"
BATCH = "https://api.osv.dev/v1/querybatch"
VULNS = "https://api.osv.dev/v1/vulns"
CRIT, MOD = "GHSA-xvch-5gv4-984h", "GHSA-vh95-rmgr-6w4m"


def load(name: str) -> dict:
    return json.loads((OSV / f"{name}.json").read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


# --- severity and fixed versions (recorded OSV records) -----------------------------------------


def test_cvss3_scores():
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:L") == 5.6
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H") == 10.0
    assert cvss3_base_score("CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    assert cvss3_base_score("garbage") is None
    assert cvss3_base_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None


@pytest.mark.parametrize(
    "score,label",
    [(0.0, "low"), (3.9, "low"), (4.0, "medium"), (6.9, "medium"), (7.0, "high"), (8.9, "high"),
     (9.0, "critical"), (10, "critical")],
)
def test_severity_bands(score, label):
    assert severity_from_score(score) == label


def test_severity_from_recorded_records():
    assert severity_of(load(CRIT))[0] == "critical"
    assert severity_of(load(MOD))[0] == "medium"  # GitHub's MODERATE maps to medium


def test_severity_falls_back_to_cvss_then_unknown():
    vector = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    raw = {"id": "X", "severity": [{"type": "CVSS_V3", "score": vector}]}
    assert severity_of(raw) == ("critical", vector)
    assert severity_of({"id": "X"}) == ("unknown", None)
    assert severity_of({"id": "X", "severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/..."}]})[0] == "unknown"


def test_fixed_in_from_recorded_records():
    assert fixed_in(load(CRIT), "minimist", "1.2.0") == "1.2.6"
    # two ranges (fixed 0.2.1 and 1.2.3): only fixes ahead of the current version count
    assert fixed_in(load(MOD), "minimist", "1.2.0") == "1.2.3"
    assert fixed_in(load(MOD), "minimist", "0.1.0") == "0.2.1"  # lowest fix above current
    assert fixed_in(load(CRIT), "minimist", "1.2.6") is None  # already fixed
    assert fixed_in(load(CRIT), "other-package", "1.2.0") is None


def test_fixed_in_ignores_git_ranges_and_other_ecosystems():
    def entry(eco, rtype, fixed):
        return {"package": {"name": "p", "ecosystem": eco}, "ranges": [{"type": rtype, "events": [{"fixed": fixed}]}]}

    raw = {"id": "X", "affected": [entry("npm", "GIT", "9.9.9"), entry("PyPI", "ECOSYSTEM", "8.0.0"),
                                   entry("npm", "SEMVER", "2.0.0")]}
    assert fixed_in(raw, "p", "1.0.0") == "2.0.0"


def test_min_safe_version_is_highest_fix():
    vulns = [
        PackageVuln("a", "high", None, "u", "1.2.3"),
        PackageVuln("b", "low", None, "u", "1.10.0"),
        PackageVuln("c", "low", None, "u", None),
    ]
    assert min_safe_version(vulns) == "1.10.0"  # semver order, not string order
    assert min_safe_version([PackageVuln("c", "low", None, "u", None)]) is None
    assert min_safe_version([]) is None


def test_parse_vuln():
    rec = parse_vuln(load(CRIT))
    assert rec.id == CRIT and rec.severity == "critical" and "CVE-2021-44906" in rec.aliases
    assert rec.url.startswith("https://") and rec.raw["id"] == CRIT


# --- querybatch ----------------------------------------------------------------------------------


async def qb(targets):
    async with HttpClient(wait_scale=0) as http:
        return await query_batch(http, targets)


@respx.mock
def test_batch_request_shape_and_ordering():
    route = respx.post(BATCH).respond(200, json=load("querybatch"))
    result = run(qb([("minimist", "1.2.0"), ("morgan", "1.12.1")]))
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "queries": [
            {"package": {"name": "minimist", "ecosystem": "npm"}, "version": "1.2.0"},
            {"package": {"name": "morgan", "ecosystem": "npm"}, "version": "1.12.1"},
        ]
    }
    assert {h["id"] for h in result[0]} == {CRIT, MOD} and result[1] == []


@respx.mock
def test_batch_pagination_requeries_only_paged_entries():
    calls: list[dict] = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"vulns": [{"id": "A", "modified": "m"}], "next_page_token": "t1"},
                        {"vulns": [{"id": "B", "modified": "m"}]},
                        {},
                    ]
                },
            )
        return httpx.Response(200, json={"results": [{"vulns": [{"id": "C", "modified": "m"}]}]})

    respx.post(BATCH).mock(side_effect=handler)
    result = run(qb([("p1", "1.0.0"), ("p2", "1.0.0"), ("p3", "1.0.0")]))
    assert [h["id"] for h in result[0]] == ["A", "C"]
    assert [h["id"] for h in result[1]] == ["B"] and result[2] == []
    assert len(calls) == 2 and len(calls[1]["queries"]) == 1
    assert calls[1]["queries"][0]["page_token"] == "t1"
    assert calls[1]["queries"][0]["package"]["name"] == "p1"
    assert all("purl" not in q["package"] for c in calls for q in c["queries"])


@respx.mock
def test_batch_result_count_mismatch_raises():
    respx.post(BATCH).respond(200, json={"results": [{}]})
    with pytest.raises(RuntimeError, match="1 results for 2 queries"):
        run(qb([("a", "1.0.0"), ("b", "1.0.0")]))


@respx.mock
def test_batch_splits_at_1000():
    sizes = []

    def handler(request):
        n = len(json.loads(request.content)["queries"])
        sizes.append(n)
        return httpx.Response(200, json={"results": [{} for _ in range(n)]})

    respx.post(BATCH).mock(side_effect=handler)
    run(qb([(f"p{i}", "1.0.0") for i in range(1500)]))
    assert sizes == [1000, 500]


# --- check_security with cache -----------------------------------------------------------------


@pytest.fixture
def conn(patched_db):
    c = store.connect(str(patched_db))
    store.migrate(c)
    return c


async def secure(conn, targets):
    async with HttpClient(wait_scale=0) as http:
        return await check_security(http, conn, targets)


TWO = [("minimist", "1.2.0"), ("morgan", "1.12.1")]  # matches the recorded querybatch response


def mock_osv():
    respx.post(BATCH).respond(200, json=load("querybatch"))
    return (
        respx.get(f"{VULNS}/{CRIT}").respond(200, json=load(CRIT)),
        respx.get(f"{VULNS}/{MOD}").respond(200, json=load(MOD)),
    )


@respx.mock
def test_check_security_end_to_end_and_cache(conn):
    r1, r2 = mock_osv()
    targets = [("minimist", "1.2.0"), ("morgan", "1.12.1")]
    out = run(secure(conn, targets))
    vulns = {v.id: v for v in out[("minimist", "1.2.0")]}
    assert vulns[CRIT].severity == "critical" and vulns[CRIT].fixed_in == "1.2.6"
    assert vulns[MOD].severity == "medium" and vulns[MOD].fixed_in == "1.2.3"
    assert out[("morgan", "1.12.1")] == []
    assert min_safe_version(list(vulns.values())) == "1.2.6"
    assert conn.execute("SELECT count(*) FROM vulnerabilities").fetchone()[0] == 2

    # second run: ids come back with the same `modified`, so no detail requests
    run(secure(conn, targets))
    assert r1.call_count == 1 and r2.call_count == 1


@respx.mock
def test_changed_modified_refetches_details(conn):
    r1, _ = mock_osv()
    run(secure(conn, TWO))
    conn.execute("UPDATE vulnerabilities SET modified_at = '2000-01-01T00:00:00Z' WHERE id = ?", (CRIT,))
    conn.commit()
    run(secure(conn, TWO))
    assert r1.call_count == 2


@respx.mock
def test_detail_failure_keeps_advisory_with_unknown_severity(conn):
    respx.post(BATCH).respond(200, json=load("querybatch"))
    respx.get(f"{VULNS}/{CRIT}").respond(404)
    respx.get(f"{VULNS}/{MOD}").respond(200, json=load(MOD))
    out = run(secure(conn, TWO))
    by_id = {v.id: v for v in out[("minimist", "1.2.0")]}
    assert set(by_id) == {CRIT, MOD} and by_id[CRIT].severity == "unknown"


def test_vulnerability_rows_roundtrip(conn):
    rec = VulnRecord(id="X-1", summary="s", severity="high", url="u", raw={"id": "X-1"}, modified_at="m")
    store.save_vulnerability(conn, rec)
    store.save_vulnerability(conn, rec)
    row = store.load_vulnerability(conn, "X-1")
    assert row["severity"] == "high" and row["raw"] == {"id": "X-1"}


# --- pipeline integration -------------------------------------------------------------------------


def as_package(raw: dict, name: str) -> dict:
    """The recorded advisories are about minimist; re-target them at another package for the pipeline test."""
    affected = {**raw["affected"][0], "package": {"name": name, "ecosystem": "npm"}}
    return {**raw, "affected": [affected]}


def pipeline_registry():
    return FakeRegistry(
        {
            "supertest": info("supertest", ["6.3.0", "6.3.4"]),
            "morgan": info("morgan", ["1.10.0", "1.12.0", "1.12.1"]),
            "eslint": info("eslint", ["8.0.0", "8.5.0"]),
        }
    )


async def run_pipeline(db_path):
    async with HttpClient(wait_scale=0) as http:
        return await pipeline.run_dependency_updates_async(
            str(db_path), registry=pipeline_registry(), http=http, notes=False, write_report=False
        )


@respx.mock
def test_pipeline_records_vulnerabilities(patched_db):
    reports = {"morgan": [{"id": CRIT, "modified": "m1"}, {"id": MOD, "modified": "m2"}]}

    def handler(request):
        queries = json.loads(request.content)["queries"]
        results = [{"vulns": reports.get(q["package"]["name"], [])} for q in queries]
        return httpx.Response(200, json={"results": results})

    respx.post(BATCH).mock(side_effect=handler)
    respx.get(f"{VULNS}/{CRIT}").respond(200, json=as_package(load(CRIT), "morgan"))
    respx.get(f"{VULNS}/{MOD}").respond(200, json=as_package(load(MOD), "morgan"))
    # morgan is pinned to 1.10.0; the recorded advisories were fixed in 1.2.x, long before it
    conn = store.connect(str(patched_db))
    conn.execute(
        "UPDATE edges SET properties = json_set(properties, '$.declared_constraint', '1.10.0') "
        "WHERE target_id = 'dep:npm:morgan' AND type = 'DECLARES'"
    )
    conn.commit()

    run_id, stats = run(run_pipeline(patched_db))
    rows = rows_by_name(patched_db)
    mg = rows["morgan"]
    # no fixed version above 1.10.0 exists in these records, so there is nothing to upgrade to
    assert mg["vuln_count"] == 2 and mg["max_severity"] == "critical"
    assert mg["min_safe_version"] is None
    assert mg["risk_level"] == "critical" and mg["status"] == "pending"
    assert stats.vulnerable == 1
    links = store.list_update_vulns(store.connect(str(patched_db)), mg["id"])
    assert {r["id"] for r in links} == {CRIT, MOD} and all(r["fixed_in"] is None for r in links)
    assert rows["eslint"]["vuln_count"] == 0 and rows["eslint"]["status"] == "up_to_date"
    assert store.get_pipeline_run(store.connect(str(patched_db)), run_id)["status"] == "succeeded"


@respx.mock
def test_pipeline_min_safe_version_outside_range_needs_manifest_change(patched_db):
    raw = as_package(load(CRIT), "morgan")
    raw["affected"][0]["ranges"] = [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}]
    respx.post(BATCH).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"vulns": [{"id": CRIT, "modified": "m"}] if q["package"]["name"] == "morgan" else []}
                    for q in json.loads(request.content)["queries"]
                ]
            },
        )
    )
    respx.get(f"{VULNS}/{CRIT}").respond(200, json=raw)
    run_id, stats = run(run_pipeline(patched_db))
    mg = rows_by_name(patched_db)["morgan"]
    assert mg["min_safe_version"] == "2.0.0" and mg["requires_manifest_change"] == 1
    assert mg["recommended_target"] == "1.12.1"  # fix is outside the range: fall back to latest


@respx.mock
def test_osv_outage_marks_run_partial_but_keeps_rows(patched_db):
    respx.post(BATCH).respond(503)
    run_id, stats = run(run_pipeline(patched_db))
    assert len(rows_by_name(patched_db)) == 3
    assert stats.warnings and "security check failed" in stats.warnings[0]
    assert store.get_pipeline_run(store.connect(str(patched_db)), run_id)["status"] == "partial"
