"""H6: the bot-PR comment and the dashboard / team issues are reached through the worker, behind the token and
allowlist gate."""
import asyncio
import json

import httpx
import pytest
import respx

from app.actions import repo_actions
from app.core import db as core_db
from app.core.config import get_settings
from app.updates.http import HttpClient
from tests.api.conftest import Env
from tests.e2e import golden

pytestmark = [pytest.mark.e2e, pytest.mark.node]
GH = "https://api.github.com"
SLUG = "acme/shop"


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    conn = core_db.connect(env.db)
    with conn:
        conn.execute("UPDATE scan_meta SET repo_url = ?", (f"https://github.com/{SLUG}",))
    conn.close()
    return env


def scan_id(env) -> str:
    conn = core_db.connect(env.db)
    return conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]


def mock_github_writes():
    respx.get(f"{GH}/repos/{SLUG}/pulls").respond(200, json=[
        {"number": 5, "user": {"login": "dependabot[bot]"}, "base": {"sha": "b", "ref": "main"},
         "head": {"sha": "h", "ref": "dependabot/npm_and_yarn/express-5.1.0"}}])
    manifests = {"b": {"dependencies": {"express": "4.21.2"}}, "h": {"dependencies": {"express": "^5.1.0"}}}
    respx.get(f"{GH}/repos/{SLUG}/contents/package.json").mock(
        side_effect=lambda req: httpx.Response(200, text=json.dumps(manifests[req.url.params["ref"]])))
    respx.get(f"{GH}/repos/{SLUG}/issues/5/comments").respond(200, json=[])
    comment = respx.post(f"{GH}/repos/{SLUG}/issues/5/comments").respond(201, json={"id": 1})
    respx.get(f"{GH}/repos/{SLUG}/issues").respond(200, json=[])
    respx.post(f"{GH}/repos/{SLUG}/labels").respond(422, json={})
    issue = respx.post(f"{GH}/repos/{SLUG}/issues").respond(201, json={"number": 9, "html_url": "https://x/9"})
    return comment, issue


def with_github_writes(monkeypatch) -> dict:
    """Env.work opens its own respx session, so the routes are registered from inside it."""
    routes: dict = {}
    original = golden.mock_github

    def both():
        original()
        routes["comment"], routes["issue"] = mock_github_writes()

    monkeypatch.setattr(golden, "mock_github", both)
    return routes


def test_job_comments_on_the_bot_pr_and_publishes_the_dashboard_issue(env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", SLUG)
    routes = with_github_writes(monkeypatch)
    handled = env.work(("repo_actions", {"scan_id": scan_id(env)}))
    comment, issue = routes["comment"], routes["issue"]
    assert handled == 1
    assert comment.call_count == 1 and "express 4.21.2 → 5.3.0" in json.loads(comment.calls[0].request.content)["body"]
    assert issue.called  # dashboard issue (and the team issue when CODEOWNERS names one)
    conn = core_db.connect(env.db)
    assert conn.execute("SELECT status FROM jobs WHERE kind = 'repo_actions'").fetchone()["status"] == "succeeded"


def test_scan_queues_repo_actions_only_with_a_token(env, monkeypatch):
    conn = core_db.connect(env.db)
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'repo_actions'").fetchone()[0] == 0  # no token

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", SLUG)
    with_github_writes(monkeypatch)
    env.work(("dependency_updates", {"scan_id": scan_id(env), "only": ["express"]}))
    # a token alone is not consent to post on GitHub after every scan (ground rule 8)
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'repo_actions'").fetchone()[0] == 0

    monkeypatch.setenv("PILOT_REPO_ACTIONS", "1")
    with conn:
        conn.execute("DELETE FROM jobs WHERE kind = 'dependency_updates'")
    env.work(("dependency_updates", {"scan_id": scan_id(env), "only": ["express"]}))
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'repo_actions'").fetchone()[0] == 1


def test_gate_names_the_reason(env, monkeypatch):
    conn = core_db.connect(env.db)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert repo_actions.eligible_repo(conn, scan_id(env), get_settings())[1] == "no GITHUB_TOKEN"
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "someone/else")
    repo, reason = repo_actions.eligible_repo(conn, scan_id(env), get_settings())
    assert repo is None and "EXEC_ALLOWED_REPOS" in reason
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", SLUG)
    assert repo_actions.eligible_repo(conn, scan_id(env), get_settings()) == (SLUG, "")

    async def skipped():
        async with HttpClient(wait_scale=0) as http:
            monkeypatch.setenv("EXEC_ALLOWED_REPOS", "someone/else")
            return await repo_actions.run_repo_actions(conn, scan_id(env), get_settings(), http)

    assert "skipped" in asyncio.run(skipped())  # nothing is sent to a repository Pilot may not act on


def test_one_failing_action_does_not_stop_the_other(env, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", SLUG)
    conn = core_db.connect(env.db)

    async def go():
        async with HttpClient(wait_scale=0) as http:
            with respx.mock:
                respx.get(f"{GH}/repos/{SLUG}/pulls").respond(500, json={})  # bot PRs: unreadable -> handled as none
                respx.get(f"{GH}/repos/{SLUG}/issues").respond(200, json=[])
                respx.post(f"{GH}/repos/{SLUG}/labels").respond(422, json={})
                respx.post(f"{GH}/repos/{SLUG}/issues").respond(500, json={"message": "boom"})  # issues fail
                with pytest.raises(RuntimeError, match="issues"):
                    await repo_actions.run_repo_actions(conn, scan_id(env), get_settings(), http)

    asyncio.run(go())
