import asyncio

import pytest
import respx

from app.actions import issues
from app.actions.codeowners import parse_codeowners
from app.core import db as core_db
from app.updates.http import HttpClient
from tests.api.conftest import Env

pytestmark = [pytest.mark.e2e, pytest.mark.node]
GH = "https://api.github.com"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def seeded_conn(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    env = Env(tmp_path_factory.mktemp("issues"), mp)
    env.work(("scan", {"repo_url": str(env.origin)}))
    conn = core_db.connect(env.db)
    yield env, conn
    mp.undo()


def test_collect_and_dashboard_rendering(seeded_conn):
    env, conn = seeded_conn
    scan = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    items = issues.collect(conn, scan)
    assert [i.package for i in items] == ["express"]  # up-to-date dependencies are left out
    item = items[0]
    assert (item.risk, item.reachability, item.status) == ("critical", "reachable_in_code", "Awaiting approval")
    assert item.lines == 7 and "routes/orders.js" in item.files and "lib/notify.js" not in item.files

    text = issues.render_dashboard(items, "pilot-sandbox-shop", "2026-10-28 10:00:00")
    assert text.startswith(issues.DASHBOARD_MARKER)
    assert "| critical | express | 4.21.2 → 5.3.0 | reachable in code | Awaiting approval | 7 lines in 5 files | - |" in text


def test_team_grouping_follows_codeowners(seeded_conn):
    _, conn = seeded_conn
    scan = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    items = issues.collect(conn, scan)
    rules = parse_codeowners("* @acme/team-shop\nroutes/admin.js @acme/platform\nroutes/i18n.js @acme/platform\n"
                             "routes/fallback.js @acme/platform\n")
    teams = issues.group_by_team(items, rules)
    assert list(teams) == ["@acme/platform"]  # platform owns 3 of the 5 affected files, team-shop 2
    assert issues.group_by_team(items, []) == {}
    body = issues.render_team("@acme/platform", teams["@acme/platform"], "pilot-sandbox-shop")
    assert issues.team_marker("@acme/platform") in body and "- `routes/orders.js`" in body


def test_without_a_token_issues_are_written_to_files(seeded_conn):
    env, conn = seeded_conn
    scan = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    published = run(issues.publish_issues(conn, scan))
    assert [p["kind"] for p in published] == ["dashboard", "team"] and all(p["state"] == "local" for p in published)
    text = open(published[0]["url"], encoding="utf-8").read()
    assert text.startswith("# Dependency dashboard (pilot-sandbox-shop)")
    assert issues.DASHBOARD_MARKER in text
    again = run(issues.publish_issues(conn, scan))  # idempotent: same files, nothing new
    assert [p["url"] for p in again] == [p["url"] for p in published]


@respx.mock
def test_with_a_token_the_dashboard_issue_is_created_once_and_then_updated(seeded_conn, monkeypatch):
    _, conn = seeded_conn
    scan = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    conn.execute("UPDATE scan_meta SET repo_url = 'https://github.com/acme/shop' WHERE scan_id = ?", (scan,))
    conn.commit()
    respx.post(f"{GH}/repos/acme/shop/labels").respond(422, json={})
    listing = respx.get(f"{GH}/repos/acme/shop/issues").respond(200, json=[])
    create = respx.post(f"{GH}/repos/acme/shop/issues").respond(201, json={"number": 1, "html_url": "https://x/1"})

    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await issues.publish_issues(conn, scan, http, teams=False)

    assert run(go())[0]["state"] == "open" and create.call_count == 1
    listing.respond(200, json=[{"number": 1, "body": issues.DASHBOARD_MARKER + "\nold"}])
    patch = respx.patch(f"{GH}/repos/acme/shop/issues/1").respond(200, json={"number": 1, "html_url": "https://x/1"})
    assert run(go())[0]["state"] == "updated" and patch.called and create.call_count == 1
