"""S5: Dependabot alerts, merged with OSV and turned into change events."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.changes import store as change_store
from app.changes.sources import dependabot
from app.channels import store as channel_store
from app.core import db as core_db
from app.scan import sbom
from app.scan import store as scan_store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient

SANDBOX = Path(__file__).resolve().parents[3] / "pilot-sandbox-shop"
GH = "https://api.github.com"
SLUG = "acme/shop"
REPO = "https://github.com/acme/shop"


def alert(number, package, ghsa, cve=None, severity="high", rng=">= 6.0.0, < 6.14.0", fixed="6.14.0", scope="runtime"):
    return {
        "number": number, "state": "open", "html_url": f"https://github.com/acme/shop/security/dependabot/{number}",
        "dependency": {"package": {"ecosystem": "npm", "name": package}, "manifest_path": "package-lock.json",
                       "scope": scope, "relationship": "transitive"},
        "security_advisory": {"ghsa_id": ghsa, "cve_id": cve, "summary": f"{package} problem {number}",
                              "severity": severity, "vulnerabilities": []},
        "security_vulnerability": {"package": {"ecosystem": "npm", "name": package}, "severity": severity,
                                   "vulnerable_version_range": rng, "first_patched_version": {"identifier": fixed}},
    }


@pytest.fixture
def scanned(sandbox_origin, tmp_path):
    from app.orchestrator.ingest import ingest

    db = str(tmp_path / "t.db")
    scan_id = ingest(str(sandbox_origin), db, tmp_path / "ws")
    conn = core_db.connect(db)
    components, _ = sbom.build_sbom(SANDBOX)
    scan_store.replace_components(conn, scan_id, components)
    qs = next(c for c in components if c.name == "qs")
    scan_store.replace_scan_vulns(conn, scan_id, [{
        "vuln_id": "GHSA-qs", "name": "qs", "version": qs.version, "purl": qs.purl, "scope": "runtime", "direct": False,
        "path": qs.path, "severity": "high", "summary": "qs problem", "aliases": ["CVE-2026-1"], "fixed_in": "6.14.0",
        "verdict": "applies", "reason": "r", "sources": ["osv"], "url": "https://osv.dev/GHSA-qs"}])
    return conn, scan_id, qs


def run(coro):
    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await coro(GitHubClient(http, "tok"))

    return asyncio.run(go())


# --- reading alerts ---------------------------------------------------------------------------------------------------


@respx.mock
def test_pages_are_followed_through_the_link_header():
    link = f'<{GH}/repos/{SLUG}/dependabot/alerts?per_page=100&after=CUR1>; rel="next"'
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts", params={"state": "open", "ecosystem": "npm", "per_page": "100"}
              ).respond(200, json=[alert(1, "qs", "GHSA-1")], headers={"link": link})
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts", params={"per_page": "100", "after": "CUR1"}
              ).respond(200, json=[alert(2, "debug", "GHSA-2")])
    alerts = run(lambda client: dependabot.fetch_alerts(client, SLUG))
    assert [a["number"] for a in alerts] == [1, 2]


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
def test_unreadable_alerts_raise_a_plain_reason(status):
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts").respond(status, json={"message": "nope"})
    with pytest.raises(dependabot.AlertsUnavailable, match="Dependabot alerts"):
        run(lambda client: dependabot.fetch_alerts(client, SLUG))


@respx.mock
def test_server_errors_are_not_swallowed():
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts").respond(500, json={})
    with pytest.raises((httpx.HTTPStatusError, Exception)):
        run(lambda client: dependabot.fetch_alerts(client, SLUG))


# --- merging ----------------------------------------------------------------------------------------------------------


def test_alert_matching_an_osv_row_adds_a_source_and_unmatched_alerts_are_added(scanned):
    conn, scan_id, qs = scanned
    matching = alert(1, "qs", "GHSA-other", cve="CVE-2026-1")  # the same advisory under its CVE alias
    new = alert(2, "debug", "GHSA-dd", severity="medium", rng=">= 0", fixed="2.6.10")
    counts = dependabot.merge_into_scan(conn, scan_id, [matching, new])
    assert counts == {"merged": 1, "added": 1}
    rows = {r["vuln_id"]: r for r in scan_store.list_scan_vulns(conn, scan_id)}
    assert rows["GHSA-qs"]["sources"] == ["osv", "dependabot"]
    assert rows["GHSA-dd"]["sources"] == ["dependabot"] and rows["GHSA-dd"]["fixed_in"] == "2.6.10"
    assert rows["GHSA-dd"]["verdict"] in ("applies", "does_not_apply", "undetermined", "not_shipped")
    # again: both are known now, so nothing is added twice
    assert dependabot.merge_into_scan(conn, scan_id, [matching, new]) == {"merged": 2, "added": 0}
    assert len(scan_store.list_scan_vulns(conn, scan_id)) == 2


def test_a_range_that_excludes_every_installed_copy_adds_nothing(scanned):
    conn, scan_id, _ = scanned
    out = dependabot.merge_into_scan(conn, scan_id, [alert(3, "debug", "GHSA-far", rng="< 0.0.1")])
    assert out == {"merged": 0, "added": 0}


# --- events and the channel -----------------------------------------------------------------------------------------


@respx.mock
def test_sync_records_events_once_and_the_channel_status(scanned):
    conn, scan_id, qs = scanned
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts").respond(200, json=[
        alert(1, "qs", "GHSA-other", cve="CVE-2026-1"), alert(2, "debug", "GHSA-dd", severity="medium", rng=">= 0")])
    first = run(lambda client: dependabot.sync_scan(conn, client, SLUG, REPO, scan_id))
    assert first["status"] == "connected" and first["alerts"] == 2 and first["new_events"] == 2 and first["merged"] == 1
    events = change_store.list_events(conn, repo=REPO)
    assert {e["source"] for e in events} == {"dependabot_alert"} and all(e["origin"] == "external" for e in events)
    payload = next(e["payload"] for e in events if e["subject_name"] == "qs")
    assert payload["items"][0]["verified"] is True and payload["items"][0]["kind"] == "security"
    assert payload["to_version"] == "6.14.0" and payload["severity_hint"] == "high"
    assert payload["from_version"] == qs.version
    second = run(lambda client: dependabot.sync_scan(conn, client, SLUG, REPO, scan_id))
    assert second["new_events"] == 0 and len(change_store.list_events(conn, repo=REPO)) == 2
    assert channel_store.get_channel(conn, REPO, "dependabot")["status"] == "connected"


@respx.mock
def test_sync_without_permission_sets_the_channel_and_does_not_fail(scanned):
    conn, scan_id, _ = scanned
    respx.get(f"{GH}/repos/{SLUG}/dependabot/alerts").respond(403, json={})
    out = run(lambda client: dependabot.sync_scan(conn, client, SLUG, REPO, scan_id))
    assert out["status"] == "needs_permission" and out["alerts"] == 0
    channel = channel_store.get_channel(conn, REPO, "dependabot")
    assert channel["status"] == "needs_permission" and "Dependabot alerts" in channel["last_error"]


def test_event_ids_are_stable_and_json_serialisable(scanned):
    conn, scan_id, _ = scanned
    a = dependabot.alert_event(REPO, scan_id, alert(1, "qs", "GHSA-x"), "6.13.0")
    b = dependabot.alert_event(REPO, "another-scan", alert(1, "qs", "GHSA-x"), "6.13.0")
    assert a.event_id == b.event_id and len(a.event_id) == 24  # a new scan does not make a new event
    json.loads(a.model_dump_json())
    assert dependabot.alert_event(REPO, None, {"number": 9}, None) is None
