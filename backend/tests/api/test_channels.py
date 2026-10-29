"""S6: channels, webhook install, notifications, Confluence publish and schedules."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import respx

from app import notifications
from app.channels import store as channel_store
from app.core import db as core_db
from app.core.config import Settings
from app.orchestrator import schedule
from app.updates.http import HttpClient
from tests.api.test_api import client_for

pytestmark = [pytest.mark.node]
GH = "https://api.github.com"
SECRETS = ("xoxb-SECRET", "ghp_SECRET", "wh-SECRET", "cf-SECRET", "smtp-SECRET")


@pytest.fixture
def server_secrets(monkeypatch):
    for name, value in {"SLACK_BOT_TOKEN": SECRETS[0], "GITHUB_TOKEN": SECRETS[1], "GITHUB_WEBHOOK_SECRET": SECRETS[2],
                        "CONFLUENCE_API_TOKEN": SECRETS[3], "SMTP_PASSWORD": SECRETS[4],
                        "CONFLUENCE_BASE_URL": "https://acme.atlassian.net", "CONFLUENCE_EMAIL": "bot@acme.com",
                        "PILOT_PUBLIC_URL": "https://pilot.acme.com"}.items():
        monkeypatch.setenv(name, value)


def repo_id(client) -> str:
    return client.get("/api/repos").json()[0]["repo_id"]


# --- the channel API never carries a secret ----------------------------------------------------------------------------


def test_get_channels_lists_every_kind_and_contains_no_secret_value(fresh, server_secrets):
    with client_for(fresh) as client:
        rid = repo_id(client)
        text = client.get(f"/api/repos/{rid}/channels").text
        assert not any(secret in text for secret in SECRETS)
        data = json.loads(text)
        kinds = {c["kind"] for c in data["channels"]}
        assert {"slack", "email", "confluence", "github_webhook", "dependabot", "bot_prs"} <= kinds
        slack = next(c for c in data["channels"] if c["kind"] == "slack")
        assert slack["secret_ref"] == "SLACK_BOT_TOKEN" and slack["secret_set"] is True and slack["status"] == "not_connected"
        assert data["webhook"] == {"url": "https://pilot.acme.com/api/webhooks/github", "public_url_set": True,
                                   "secret_set": True, "token_set": True}


def test_config_cannot_hold_a_secret_and_secret_refs_are_allowlisted(fresh):
    with client_for(fresh) as client:
        rid = repo_id(client)
        url = f"/api/repos/{rid}/channels/slack"
        assert client.put(url, json={"config": {"channel": "#x", "api_token": "xoxb-1"}}).status_code == 422
        assert client.put(url, json={"config": {"channel": "#x"}, "secret_ref": "HOME"}).status_code == 422
        assert client.put(url, json={"config": {"channel": "#x"}, "secret_ref": "PATH"}).status_code == 422
        ok = client.put(url, json={"enabled": True, "config": {"channel": "#payments"}, "secret_ref": "PILOT_SECRET_SLACK"})
        assert ok.status_code == 200
        slack = next(c for c in ok.json()["channels"] if c["kind"] == "slack")
        assert slack["enabled"] and slack["config"] == {"channel": "#payments"} and slack["secret_ref"] == "PILOT_SECRET_SLACK"
        assert client.put(f"/api/repos/{rid}/channels/nope", json={}).status_code == 422
        assert client.post(f"/api/repos/{rid}/channels/nope/test").status_code == 404


def test_writes_need_the_token_when_one_is_set(fresh):
    with client_for(fresh, pilot_api_token="t0k") as client:
        rid = repo_id(client)
        assert client.put(f"/api/repos/{rid}/channels/slack", json={}).status_code == 401
        assert client.post(f"/api/repos/{rid}/webhook").status_code == 401
        assert client.post(f"/api/repos/{rid}/channels/slack/test").status_code == 401
        assert client.put("/api/schedules/x", json={}).status_code == 401


# --- connection tests (server side) ---------------------------------------------------------------------------------


@respx.mock
def test_slack_test_connection_uses_the_server_token(fresh, server_secrets):
    auth = respx.post("https://slack.com/api/auth.test").respond(200, json={"ok": True, "team": "Acme"})
    with client_for(fresh) as client:
        rid = repo_id(client)
        client.put(f"/api/repos/{rid}/channels/slack", json={"config": {"channel": "#payments-changes"}})
        out = client.post(f"/api/repos/{rid}/channels/slack/test").json()
        assert out["ok"] and "Acme" in out["detail"] and "#payments-changes" in out["detail"]
        assert auth.calls[0].request.headers["authorization"] == f"Bearer {SECRETS[0]}"
        auth.respond(200, json={"ok": False, "error": "invalid_auth"})
        bad = client.post(f"/api/repos/{rid}/channels/slack/test").json()
        assert bad["ok"] is False and "invalid_auth" in bad["detail"] and SECRETS[0] not in bad["detail"]
        channel = next(c for c in client.get(f"/api/repos/{rid}/channels").json()["channels"] if c["kind"] == "slack")
        assert channel["status"] == "error" and "invalid_auth" in channel["last_error"]


@respx.mock
def test_confluence_test_connection_and_missing_secrets(fresh, server_secrets, monkeypatch):
    respx.get("https://acme.atlassian.net/wiki/api/v2/spaces").respond(200, json={"results": []})
    with client_for(fresh) as client:
        rid = repo_id(client)
        client.put(f"/api/repos/{rid}/channels/confluence", json={"config": {"space_id": "123"}})
        assert client.post(f"/api/repos/{rid}/channels/confluence/test").json()["ok"] is True
        respx.get("https://acme.atlassian.net/wiki/api/v2/spaces").respond(401, json={})
        assert "rejected" in client.post(f"/api/repos/{rid}/channels/confluence/test").json()["detail"]
    monkeypatch.delenv("CONFLUENCE_API_TOKEN")
    with client_for(fresh) as client:
        assert "must be set on the server" in client.post(f"/api/repos/{rid}/channels/confluence/test").json()["detail"]


# --- webhook install ---------------------------------------------------------------------------------------------------


@respx.mock
def test_webhook_install_is_idempotent_and_explains_missing_settings(fresh, server_secrets, monkeypatch):
    slug = "acme/shop"
    hook = {"id": 7, "config": {"url": "https://pilot.acme.com/api/webhooks/github"}}
    listing = respx.get(f"{GH}/repos/{slug}/hooks").respond(200, json=[])
    create = respx.post(f"{GH}/repos/{slug}/hooks").respond(201, json=hook)
    with client_for(fresh) as client:
        first = client.post(f"/api/repos/{slug}/webhook").json()
        assert first["ok"] and "installed" in first["detail"]
        sent = json.loads(create.calls[0].request.content)
        assert sent["events"] == ["push"] and sent["config"]["url"] == "https://pilot.acme.com/api/webhooks/github"
        assert sent["config"]["content_type"] == "json" and sent["config"]["secret"] == SECRETS[2]
        listing.respond(200, json=[hook])
        again = client.post(f"/api/repos/{slug}/webhook").json()
        assert again["ok"] and "already installed" in again["detail"] and create.call_count == 1
        listing.respond(403, json={})
        assert "cannot manage webhooks" in client.post(f"/api/repos/{slug}/webhook").json()["detail"]
    monkeypatch.delenv("PILOT_PUBLIC_URL")
    with client_for(fresh) as client:
        assert "PILOT_PUBLIC_URL" in client.post(f"/api/repos/{slug}/webhook").json()["detail"]


@respx.mock
def test_confluence_publish_creates_then_updates_one_page(fresh, server_secrets):
    base = "https://acme.atlassian.net/wiki/api/v2"
    create = respx.post(f"{base}/pages").respond(200, json={"id": "555"})
    respx.get(f"{base}/pages/555").respond(200, json={"id": "555", "version": {"number": 3}})
    update = respx.put(f"{base}/pages/555").respond(200, json={"id": "555"})
    with client_for(fresh) as client:
        rid = repo_id(client)
        client.put(f"/api/repos/{rid}/channels/confluence", json={"config": {"space_id": "98765"}})
        assert client.post(f"/api/repos/{rid}/publish/confluence").json() == {"ok": True, "detail": "Report page created"}
        body = json.loads(create.calls[0].request.content)
        assert body["spaceId"] == "98765" and body["status"] == "current" and body["body"]["representation"] == "storage"
        assert "express" in body["body"]["value"] and "<table>" in body["body"]["value"]
        assert "diff" not in body["body"]["value"].lower()  # a report, not code
        assert client.post(f"/api/repos/{rid}/publish/confluence").json() == {"ok": True, "detail": "Report page updated"}
        put = json.loads(update.calls[0].request.content)
        assert put["id"] == "555" and put["version"]["number"] == 4 and create.call_count == 1
        saved = next(c for c in client.get(f"/api/repos/{rid}/channels").json()["channels"] if c["kind"] == "confluence")
        assert saved["config"]["page_id"] == "555"


# --- notifications ----------------------------------------------------------------------------------------------------


@respx.mock
def test_waiting_run_notifies_once_without_code(fresh, server_secrets):
    post = respx.post("https://slack.com/api/chat.postMessage").respond(200, json={"ok": True})
    conn = core_db.connect(fresh.db)
    run = conn.execute("SELECT * FROM upgrade_runs").fetchone()
    from app.orchestrator import store as run_store

    rid = notifications.repo_id(run_store.scan_repo(conn, run["scan_id"])["repo_url"])
    channel_store.save_channel(conn, rid, "slack", enabled=True, config={"channel": "#payments-changes"}, secret_ref=None)

    async def go():
        async with HttpClient(wait_scale=0) as http:
            first = await notifications.after_run(conn, http, Settings(_env_file=None, pilot_public_url="https://pilot.acme.com"), run["id"])
            second = await notifications.after_run(conn, http, Settings(_env_file=None), run["id"])
            return first, second

    first, second = asyncio.run(go())
    assert first == ["slack"] and second == [] and post.call_count == 1
    sent = json.loads(post.calls[0].request.content)
    assert sent["channel"] == "#payments-changes" and "express 4.21.2 to 5.3.0" in sent["text"]
    assert f"https://pilot.acme.com/upgrades/{run['id']}" in sent["text"] and "req.param" not in sent["text"]
    assert "diff" not in sent["text"] and "```" not in sent["text"]


def test_nothing_is_sent_without_a_switched_on_channel(fresh):
    conn = core_db.connect(fresh.db)
    run = conn.execute("SELECT id FROM upgrade_runs").fetchone()

    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await notifications.after_run(conn, http, Settings(_env_file=None), run["id"])

    assert asyncio.run(go()) == []


# --- schedules ---------------------------------------------------------------------------------------------------------


def test_schedules_default_edit_and_dedupe(fresh):
    with client_for(fresh) as client:
        rid = repo_id(client)
        listed = client.get(f"/api/repos/{rid}/channels").json()["schedules"]
        assert {s["source"]: s["interval_minutes"] for s in listed} == {
            "rescan": 1440, "dependabot_alerts": 30, "releases": 360, "internal_sources": 15}
        assert next(s for s in listed if s["source"] == "internal_sources")["enabled"] is False  # nothing to read yet
        sid = next(s["id"] for s in listed if s["source"] == "releases")
        assert client.put(f"/api/schedules/{sid}", json={"interval_minutes": 3}).status_code == 422  # too frequent
        assert client.put(f"/api/schedules/{sid}", json={"interval_minutes": 60}).json()["interval_minutes"] == 60
        assert client.put("/api/schedules/nope:x", json={"enabled": False}).status_code == 404
        assert client.get("/api/schedules").json()


def test_due_schedules_queue_one_job_each_and_skip_unlisted_repositories(fresh):
    conn = core_db.connect(fresh.db)
    from app.orchestrator import store as run_store

    url = run_store.known_repos(conn)[0]
    rid = notifications.repo_id(url)
    channel_store.ensure_schedules(conn, rid)
    later = datetime.now(timezone.utc) + timedelta(days=2)
    settings = Settings(_env_file=None, exec_allowed_repos=rid)
    ran = schedule.run_due_schedules(conn, settings, later)
    assert {s.split(":")[0] for s in ran} == {"rescan", "dependabot_alerts", "releases"}  # internal_sources is off
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE dedupe_key LIKE 'sched:%'")]
    assert sorted(kinds) == ["dependabot_alerts", "dependency_updates", "scan"]
    assert schedule.run_due_schedules(conn, settings, later) == []  # their next time moved on
    elsewhere = Settings(_env_file=None, exec_allowed_repos="someone/else")
    channel_store.update_schedule(conn, f"rescan:{rid}", interval_minutes=5)
    schedule.run_due_schedules(conn, elsewhere, later + timedelta(days=3))
    assert conn.execute("SELECT count(*) FROM jobs WHERE dedupe_key LIKE 'sched:rescan:%'").fetchone()[0] == 1  # not queued again
