"""B-10 and Step 8: API token on writes, signed push webhook, scheduled re-scan."""
import hashlib
import hmac
import json

import pytest

from app.core import db as core_db
from app.core.config import Settings
from app.orchestrator import schedule
from tests.api.test_api import client_for, express_run_id

pytestmark = [pytest.mark.node]
SECRET = "whsec"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push(slug="acme/shop", ref="refs/heads/main", default="main", after="abc123") -> bytes:
    return json.dumps({"ref": ref, "after": after, "repository": {
        "full_name": slug, "default_branch": default, "clone_url": f"https://github.com/{slug}.git"}}).encode()


def post_hook(client, body: bytes, event="push", signature=None):
    return client.post("/api/webhooks/github", content=body, headers={
        "X-GitHub-Event": event, "X-Hub-Signature-256": signature or sign(body), "Content-Type": "application/json"})


def scan_jobs(env):
    conn = core_db.connect(env.db)
    return [json.loads(r["payload"]) for r in conn.execute("SELECT payload FROM jobs WHERE kind = 'scan' ORDER BY rowid")]


# --- API token ----------------------------------------------------------------------------------------------------


def test_writes_need_the_token_and_the_approver_comes_from_the_header(fresh):
    with client_for(fresh, pilot_api_token="s3cret") as client:
        run_id = express_run_id(client)
        body = {"decision": "approved", "decided_by": "someone-else"}
        assert client.get("/api/upgrades").status_code == 200  # reads stay open
        assert client.post(f"/api/upgrades/{run_id}/decision", json=body).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert client.post(f"/api/upgrades/{run_id}/decision", json=body, headers=bad).status_code == 401
        assert client.post("/api/scans", json={"repo_url": "x"}).status_code == 401
        assert client.post("/api/feedback", json={}).status_code == 401
        assert client.post("/api/sources/override", json={}).status_code == 401
        good = {"Authorization": "Bearer s3cret", "X-Pilot-User": "alice@example.com"}
        assert client.post(f"/api/upgrades/{run_id}/decision", json=body, headers=good).status_code == 202
        conn = core_db.connect(fresh.db)
        job = conn.execute("SELECT payload FROM jobs WHERE kind = 'resume_upgrade'").fetchone()
        assert json.loads(job["payload"])["decided_by"] == "alice@example.com"  # not the free-text body


def test_without_a_token_the_api_stays_open_for_the_demo(fresh):
    with client_for(fresh) as client:
        run_id = express_run_id(client)
        r = client.post(f"/api/upgrades/{run_id}/decision", json={"decision": "approved", "decided_by": "bob"})
        assert r.status_code == 202


# --- webhook ------------------------------------------------------------------------------------------------------


def test_webhook_is_off_without_a_secret(seeded):
    with client_for(seeded) as client:
        assert post_hook(client, push()).status_code == 404


def test_webhook_checks_the_signature_and_ignores_what_it_should(seeded):
    before = len(scan_jobs(seeded))
    with client_for(seeded, github_webhook_secret=SECRET, exec_allowed_repos="acme/shop") as client:
        body = push()
        assert post_hook(client, body, signature=sign(body, "wrong")).status_code == 401
        assert client.post("/api/webhooks/github", content=body, headers={"X-GitHub-Event": "push"}).status_code == 401
        assert post_hook(client, b"{}", event="ping").json() == {"status": "pong"}
        assert post_hook(client, body, event="issues").json()["status"] == "ignored"
        other_branch = push(ref="refs/heads/feature")
        assert "not the default branch" in post_hook(client, other_branch).json()["reason"]
        foreign = push(slug="evil/repo")
        assert "EXEC_ALLOWED_REPOS" in post_hook(client, foreign).json()["reason"]
        assert post_hook(client, b"not json", event="push").status_code == 400
    assert len(scan_jobs(seeded)) == before  # nothing was queued by any of those


def test_a_signed_push_to_the_default_branch_queues_one_scan(seeded):
    before = len(scan_jobs(seeded))
    with client_for(seeded, github_webhook_secret=SECRET, exec_allowed_repos="acme/shop") as client:
        first = post_hook(client, push(after="c0ffee"))
        again = post_hook(client, push(after="c0ffee"))  # GitHub redelivers: same commit, same job
        assert first.status_code == 202 and first.json()["status"] == "queued"
        assert again.json()["job_id"] == first.json()["job_id"]
        post_hook(client, push(after="beef"))
    jobs_after = scan_jobs(seeded)
    assert len(jobs_after) == before + 2 and jobs_after[-1]["repo_url"] == "https://github.com/acme/shop.git"


# --- scheduled re-scan --------------------------------------------------------------------------------------------


def test_rescan_queues_each_allowed_known_repository_once_per_interval(seeded):
    conn = core_db.connect(seeded.db)
    repo = conn.execute("SELECT repo_url FROM scan_meta").fetchone()["repo_url"]
    allowed = Settings(_env_file=None, exec_allowed_repos=repo, pilot_rescan_minutes=10)
    first = schedule.enqueue_rescans(conn, allowed, now=1_000_000)
    assert len(first) == 1
    assert schedule.enqueue_rescans(conn, allowed, now=1_000_100) == first  # same 10-minute bucket
    later = schedule.enqueue_rescans(conn, allowed, now=1_000_000 + 700)
    assert later != first
    nothing = Settings(_env_file=None, exec_allowed_repos="someone/else", pilot_rescan_minutes=10)
    assert schedule.enqueue_rescans(conn, nothing, now=5_000_000) == []  # never outside the allowlist
    with conn:
        conn.execute("DELETE FROM jobs WHERE dedupe_key LIKE 'rescan:%'")
