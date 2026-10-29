"""S1: SBOM from the lockfile and advisory verdicts."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import db as core_db
from app.scan import sbom, vulns
from app.scan import store as scan_store
from app.scan.sbom import Component
from app.updates.security import PackageVuln

SANDBOX = Path(__file__).resolve().parents[3] / "pilot-sandbox-shop"


def write_repo(tmp_path, lock: dict, manifest: dict | None = None) -> Path:
    (tmp_path / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps(manifest or {"name": "demo", "version": "1.0.0"}), encoding="utf-8")
    return tmp_path


def pkg(version, **kw):
    return {"version": version, **kw}


# --- the sandbox's real lockfile ----------------------------------------------------------------------------------


def test_sandbox_lockfile_gives_every_package_with_paths_and_scopes():
    components, bom = sbom.build_sbom(SANDBOX)
    by_name = {c.name: c for c in components}
    assert len(components) == 159 and sum(c.direct for c in components) == 3
    assert {c.name for c in components if c.direct} == {"express", "mocha", "supertest"}
    assert by_name["express"].scope == "runtime" and by_name["express"].path == ["express"]
    assert by_name["qs"].scope == "runtime" and by_name["qs"].path[0] == "express" and by_name["qs"].path[-1] == "qs"
    assert by_name["form-data"].scope == "dev" and by_name["form-data"].path[0] == "supertest"
    assert all(c.purl.startswith("pkg:npm/") and c.purl.endswith(f"@{c.version}") for c in components)


def test_cyclonedx_document_has_the_required_fields():
    components, bom = sbom.build_sbom(SANDBOX)
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6" and bom["version"] == 1
    assert bom["serialNumber"].startswith("urn:uuid:")
    assert bom["metadata"]["component"]["type"] == "application" and bom["metadata"]["component"]["name"]
    refs = {c["bom-ref"] for c in bom["components"]}
    assert refs == {c.purl for c in components}
    for c in bom["components"]:
        assert c["type"] == "library" and c["purl"] == c["bom-ref"] and c["scope"] in ("required", "optional", "excluded")
        assert c["name"] and c["version"]
    root = bom["metadata"]["component"]["bom-ref"]
    graph = {d["ref"]: d["dependsOn"] for d in bom["dependencies"]}
    assert set(graph) == refs | {root} and all(set(d) <= refs for d in graph.values())
    assert len(graph[root]) == 3  # express, mocha, supertest
    assert bom == sbom.build_sbom(SANDBOX)[1] | {"metadata": bom["metadata"]}  # deterministic apart from the timestamp


@pytest.mark.node
def test_component_set_matches_npm_sbom():
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("npm not installed")
    done = subprocess.run([npm, "sbom", "--sbom-format", "cyclonedx", "--package-lock-only"], cwd=SANDBOX,
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        pytest.skip(f"npm sbom unavailable: {done.stderr[:100]}")
    theirs = {(c["name"], c["version"]) for c in json.loads(done.stdout)["components"]}
    ours = {(c.name, c.version) for c in sbom.build_sbom(SANDBOX)[0]}
    assert ours == theirs


# --- lockfile shapes -------------------------------------------------------------------------------------------------


def test_nested_node_modules_resolve_the_way_npm_does(tmp_path):
    repo = write_repo(tmp_path, {"lockfileVersion": 3, "packages": {
        "": {"name": "demo", "dependencies": {"a": "^1", "c": "^1"}},
        "node_modules/a": pkg("1.0.0", dependencies={"b": "^2"}),
        "node_modules/a/node_modules/b": pkg("2.0.0"),
        "node_modules/b": pkg("1.0.0"),
        "node_modules/c": pkg("1.0.0", dependencies={"b": "^1"}),
    }})
    components, _ = sbom.build_sbom(repo)
    found = {(c.name, c.version): c for c in components}
    assert found[("b", "2.0.0")].path == ["a", "b"] and not found[("b", "2.0.0")].direct  # nested copy
    assert found[("b", "1.0.0")].path == ["c", "b"] and not found[("b", "1.0.0")].direct
    assert found[("a", "1.0.0")].direct and found[("c", "1.0.0")].direct
    assert found[("a", "1.0.0")].depends_on == [sbom.make_purl("b", "2.0.0")]
    assert found[("c", "1.0.0")].depends_on == [sbom.make_purl("b", "1.0.0")]


def test_scoped_names_and_flags(tmp_path):
    repo = write_repo(tmp_path, {"lockfileVersion": 3, "packages": {
        "": {"name": "demo", "dependencies": {"@acme/money": "^1"}, "devDependencies": {"t": "^1"}},
        "node_modules/@acme/money": pkg("1.2.3", optionalDependencies={"o": "^1"}),
        "node_modules/o": pkg("1.0.0", optional=True),
        "node_modules/t": pkg("1.0.0", dev=True, peerDependencies={"p": "^1"}),
        "node_modules/p": pkg("1.0.0", peer=True),
    }})
    by_name = {c.name: c for c in sbom.build_sbom(repo)[0]}
    assert by_name["@acme/money"].purl == "pkg:npm/%40acme/money@1.2.3" and by_name["@acme/money"].direct
    assert by_name["o"].scope == "optional" and by_name["t"].scope == "dev" and by_name["p"].scope == "peer"
    assert by_name["t"].direct and by_name["o"].path == ["@acme/money", "o"]


def test_version_1_lockfile(tmp_path):
    repo = write_repo(tmp_path, {"lockfileVersion": 1, "name": "demo", "version": "1.0.0", "dependencies": {
        "a": {"version": "1.0.0", "requires": {"b": "^2"}, "dependencies": {"b": {"version": "2.0.0"}}},
        "b": {"version": "1.0.0", "dev": True},
    }})
    found = {(c.name, c.version): c for c in sbom.build_sbom(repo)[0]}
    assert found[("b", "2.0.0")].path == ["a", "b"] and found[("b", "2.0.0")].scope == "runtime"
    assert found[("b", "1.0.0")].scope == "dev" and found[("a", "1.0.0")].direct


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_missing_or_broken_lockfile_gives_a_minimal_bom(tmp_path, content):
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    if content is not None:
        (tmp_path / "package-lock.json").write_text(content, encoding="utf-8")
    components, bom = sbom.build_sbom(tmp_path)
    assert components == [] and bom["components"] == [] and bom["specVersion"] == "1.6"
    assert bom["metadata"]["properties"][0]["name"] == "pilot:warning"


def test_write_sbom_names_the_file_after_the_scan(tmp_path):
    _, bom = sbom.build_sbom(SANDBOX)
    path = sbom.write_sbom(bom, tmp_path / "out", "scan-1")
    assert path.name == "scan-1.cdx.json" and json.loads(path.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"


# --- verdicts ----------------------------------------------------------------------------------------------------------


def comp(name, path, scope="runtime", direct=False, version="1.0.0"):
    return Component(purl=sbom.make_purl(name, version), name=name, version=version, scope=scope, direct=direct,
                     path=path, location=f"node_modules/{name}")


def test_verdict_rules():
    imported = {"express"}
    assert vulns.verdict(comp("qs", ["express", "qs"]), imported)[0] == "applies"
    assert vulns.verdict(comp("express", ["express"], direct=True), imported)[0] == "applies"
    assert vulns.verdict(comp("debug", ["other", "debug"]), imported)[0] == "does_not_apply"
    assert vulns.verdict(comp("form-data", ["supertest", "form-data"], scope="dev"), imported)[0] == "not_shipped"
    assert vulns.verdict(comp("qs", ["express", "qs"]), set())[0] == "undetermined"  # nothing imported at all
    named = {"affected": [{"package": {"name": "qs"}, "ecosystem_specific": {"affected_functions": ["parse"]}}]}
    decided, reason = vulns.verdict(comp("qs", ["express", "qs"]), imported, named)
    assert decided == "undetermined" and "symbol index" in reason
    assert vulns.verdict(comp("qs", ["express", "qs"]), imported, {"affected": [{"package": {"name": "qs"}}]})[0] == "applies"


def test_dev_scope_wins_over_imports():
    assert vulns.verdict(comp("x", ["express", "x"], scope="dev"), {"express"})[0] == "not_shipped"


# --- end to end on a scanned sandbox -------------------------------------------------------------------------------------


@pytest.fixture
def scanned(sandbox_origin, tmp_path):
    from app.orchestrator.ingest import ingest

    db = str(tmp_path / "t.db")
    scan_id = ingest(str(sandbox_origin), db, tmp_path / "ws")
    conn = core_db.connect(db)
    return conn, scan_id


def test_scan_vulnerabilities_stores_one_row_per_advisory_and_package(scanned, monkeypatch):
    conn, scan_id = scanned
    components, _ = sbom.build_sbom(SANDBOX)
    scan_store.replace_components(conn, scan_id, components)
    assert [c.name for c in scan_store.load_components(conn, scan_id)] == [c.name for c in components]

    from app.updates import store as updates_store
    from app.updates.models import VulnRecord

    for vid, summary in (("GHSA-qs", "qs problem"), ("GHSA-ff", "form-data problem")):
        updates_store.save_vulnerability(conn, VulnRecord(id=vid, aliases=["CVE-1"], summary=summary, severity="high",
                                                          cvss_vector=None, published_at=None, modified_at="m", url="https://osv.dev/x", raw={}))
    qs = next(c for c in components if c.name == "qs")
    ff = next(c for c in components if c.name == "form-data")

    async def fake_check(http, db, targets, ecosystem="npm"):
        return {(qs.name, qs.version): [PackageVuln("GHSA-qs", "high", "qs problem", "https://osv.dev/GHSA-qs", "6.14.0")],
                (ff.name, ff.version): [PackageVuln("GHSA-ff", "medium", "form-data problem", "https://osv.dev/GHSA-ff", None)]}

    monkeypatch.setattr(vulns, "check_security", fake_check)
    rows = asyncio.run(vulns.scan_vulnerabilities(conn, None, scan_id))
    by_id = {r["vuln_id"]: r for r in rows}
    assert by_id["GHSA-qs"]["verdict"] == "applies" and by_id["GHSA-qs"]["path"][0] == "express"
    assert by_id["GHSA-ff"]["verdict"] == "not_shipped" and by_id["GHSA-ff"]["scope"] == "dev"
    assert by_id["GHSA-qs"]["aliases"] == ["CVE-1"] and by_id["GHSA-qs"]["fixed_in"] == "6.14.0"

    asyncio.run(vulns.scan_vulnerabilities(conn, None, scan_id))  # again: replaced, not duplicated
    assert conn.execute("SELECT count(*) FROM scan_vulns WHERE scan_id = ?", (scan_id,)).fetchone()[0] == 2
    listed = scan_store.list_scan_vulns(conn, scan_id)
    assert [r["vuln_id"] for r in listed] == ["GHSA-qs", "GHSA-ff"]  # severity, then verdict


def test_no_components_means_no_advisories(scanned):
    conn, scan_id = scanned
    assert asyncio.run(vulns.scan_vulnerabilities(conn, None, scan_id, [])) == []
