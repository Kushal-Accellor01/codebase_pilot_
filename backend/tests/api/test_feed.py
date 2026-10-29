"""S7: the update feed on the common change event."""
import subprocess

import pytest

from app.changes import feed
from app.changes import store as change_store
from app.core import db as core_db
from app.scan import store as scan_store
from tests.api.test_api import client_for

pytestmark = [pytest.mark.node]


def cards(client, **params) -> dict:
    return client.get("/api/changes", params=params).json()


def express(client) -> dict:
    return next(c for c in cards(client)["cards"] if c["subject"] == "express" and c["source"] == "registry")


def test_express_card_has_quote_impact_and_five_of_six_steps(fresh):
    with client_for(fresh) as client:
        feed_data = cards(client)
        card = express(client)
        assert card["origin"] == "external" and card["source_label"] == "New release" and card["severity"] == "critical"
        assert card["title"] == "express 4.21.2 → 5.3.0 changes APIs your code uses"
        assert card["impact"].startswith("7 lines in 5 files use APIs that change.")
        assert "pass with Pilot's fix" in card["impact"]
        assert card["quote"]["verified"] is True and "checked word for word" in card["quote"]["source"]
        assert card["quote"]["text"] and card["quote"]["url"]
        assert (card["step"], card["step_label"]) == (5, "awaiting approval")
        assert card["tracker"] == ["done", "done", "done", "done", "now", "todo"]
        assert card["action"] == {"kind": "approve", "label": "Review and approve", "run_id": card["run_id"]}
        assert feed_data["counts"]["needs_decision"] == 1 and feed_data["counts"]["outside"] >= 1


def test_a_rescan_adds_no_duplicate_events(fresh):
    with client_for(fresh) as client:
        before = cards(client)["counts"]["all"]
        fresh.work(("scan", {"repo_url": str(fresh.origin), "only": ["express"]}))
        assert cards(client)["counts"]["all"] == before
        conn = core_db.connect(fresh.db)
        assert conn.execute("SELECT count(*) FROM change_events WHERE source = 'registry'").fetchone()[0] == 1


def test_filters_and_detail(fresh):
    with client_for(fresh) as client:
        assert [c["subject"] for c in cards(client, filter="needs_decision")["cards"]] == ["express"]
        assert cards(client, filter="inside")["cards"] == []
        assert cards(client, filter="security")["cards"] == []
        assert cards(client, origin="external")["cards"]
        assert client.get("/api/changes", params={"origin": "sideways"}).status_code == 422
        assert client.get("/api/changes", params={"filter": "bogus"}).status_code == 422
        event_id = express(client)["event_id"]
        assert client.get(f"/api/changes/{event_id}").json()["event_id"] == event_id
        assert client.get("/api/changes/nope").status_code == 404


def test_approval_moves_the_card_to_step_six(fresh):
    with client_for(fresh) as client:
        run_id = express(client)["run_id"]
        client.post(f"/api/upgrades/{run_id}/decision", json={"decision": "approved", "decided_by": "alice"})
        fresh.work()
        card = express(client)
        assert (card["step"], card["step_label"]) == (6, "pull request opened")
        assert card["tracker"] == ["done"] * 5 + ["now"] and card["action"]["kind"] == "pull_request"


def test_a_merged_upgrade_closes_its_card(fresh):
    with client_for(fresh) as client:
        run_id = express(client)["run_id"]
        client.post(f"/api/upgrades/{run_id}/decision", json={"decision": "approved", "decided_by": "alice"})
        fresh.work()
        subprocess.run(["git", "-C", str(fresh.origin), "merge", "--no-edit", "pilot/express-5.3.0"], check=True,
                       capture_output=True)
        fresh.work(("scan", {"repo_url": str(fresh.origin), "only": ["express"]}))
        card = express(client)
        assert (card["step"], card["step_label"]) == (6, "done, resolved") and card["tracker"] == ["done"] * 6
        assert cards(client)["counts"]["needs_decision"] == 0


def test_applying_advisories_become_security_cards(fresh):
    conn = core_db.connect(fresh.db)
    scan_id = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    row = {"vuln_id": "GHSA-qs", "name": "qs", "version": "6.13.0", "purl": "pkg:npm/qs@6.13.0", "scope": "runtime",
           "direct": False, "path": ["express", "qs"], "severity": "high", "summary": "qs problem", "aliases": [],
           "fixed_in": "6.14.0", "verdict": "applies", "reason": "Your code imports express, which loads qs.",
           "sources": ["osv"], "url": "https://osv.dev/GHSA-qs"}
    both = {**row, "vuln_id": "GHSA-both", "sources": ["osv", "dependabot"]}
    hidden = {**row, "vuln_id": "GHSA-dev", "verdict": "not_shipped"}
    scan_store.replace_scan_vulns(conn, scan_id, [row, both, hidden])
    assert feed.record_scan_changes(conn, scan_id) == 1  # only the OSV-only advisory that applies
    assert feed.record_scan_changes(conn, scan_id) == 0
    with client_for(fresh) as client:
        data = cards(client, filter="security")
        assert data["counts"]["security"] == 1
        card = data["cards"][0]
        assert card["source_label"] == "Security advisory" and card["severity"] == "high"
        assert card["title"] == "GHSA-qs in qs reaches your code"
        assert "Applies: Your code imports express" in card["impact"] and "Fixed in 6.14.0" in card["impact"]
        assert card["quote"]["source"] == "From the advisory" and card["step"] == 1


def test_event_ids_do_not_depend_on_the_scan():
    a = change_store.make_event_id("registry", "r", "package", "express", "4.21.2", "5.3.0")
    assert a == change_store.make_event_id("registry", "r", "package", "express", "4.21.2", "5.3.0")
    assert a != change_store.make_event_id("osv", "r", "package", "express", "4.21.2", "5.3.0") and len(a) == 24
