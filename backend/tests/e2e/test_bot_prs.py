import asyncio
import base64
import json

import httpx
import pytest
import respx

from app.actions import bot_prs
from app.core import db as core_db
from app.orchestrator import store as run_store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from tests.api.conftest import Env

pytestmark = [pytest.mark.e2e, pytest.mark.node]
GH = "https://api.github.com"
REPO = "acme/shop"
AUTHORS = ["dependabot[bot]", "renovate[bot]"]


def run(coro):
    return asyncio.run(coro)


def b64(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode(), "sha": "filesha"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    return env


def pull(number=5, login="dependabot[bot]"):
    return {"number": number, "user": {"login": login}, "base": {"sha": "basesha", "ref": "main"},
            "head": {"sha": "headsha", "ref": "dependabot/npm_and_yarn/express-5.1.0"}}


def mock_bot_pr(prs, head_constraint="^5.1.0", comments=None):
    respx.get(f"{GH}/repos/{REPO}/pulls").respond(200, json=prs)
    manifests = {"basesha": {"dependencies": {"express": "4.21.2"}}, "headsha": {"dependencies": {"express": head_constraint}}}
    respx.get(f"{GH}/repos/{REPO}/contents/package.json").mock(
        side_effect=lambda req: httpx.Response(200, text=json.dumps(manifests[req.url.params["ref"]])))
    respx.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(200, json=comments or [])
    return (respx.post(f"{GH}/repos/{REPO}/issues/5/comments").respond(201, json={"id": 1}),
            respx.patch(f"{GH}/repos/{REPO}/issues/comments/99").respond(200, json={"id": 99}))


async def go(env, mode):
    conn = core_db.connect(env.db)
    async with HttpClient(wait_scale=0) as http:
        return await bot_prs.enrich_bot_prs(conn, GitHubClient(http, "t"), repo=REPO, mode=mode, bot_authors=AUTHORS)


def test_pure_helpers():
    assert bot_prs.manifest_changes(
        {"dependencies": {"a": "1.0.0", "b": "2.0.0"}, "devDependencies": {"c": "^1"}},
        {"dependencies": {"a": "1.1.0", "b": "2.0.0", "n": "^1.0.0"}, "devDependencies": {"c": "^1"}},
    ) == [("a", "1.0.0", "1.1.0"), ("n", None, "^1.0.0")]
    assert bot_prs.version_of("^5.1.0") == "5.1.0" and bot_prs.version_of("~5") == "5.0.0" and bot_prs.version_of("latest") is None


@respx.mock
def test_comment_only_creates_one_comment_with_the_impact(env):
    create, _ = mock_bot_pr([pull(), pull(6, "some-human")])
    result = run(go(env, "comment_only"))
    assert [(r["pr"], r["package"], r["action"]) for r in result] == [(5, "express", "created")]
    body = json.loads(create.calls[0].request.content)["body"]
    assert body.startswith(bot_prs.MARKER) and "express 4.21.2 → 5.3.0" in body
    assert "| `routes/orders.js` | 8 | `req.param` |" in body and "Tests on the new version without code changes: fail" in body
    assert "<details><summary>Verified fix (diff)</summary>" in body and "pushes the fix to this branch only after" not in body
    conn = core_db.connect(env.db)
    run_id = result[0]["run_id"]
    assert any(e.node == "bot_pr" for e in __import__("app.core.events", fromlist=["x"]).list_events(conn, run_id))


@respx.mock
def test_existing_comment_is_edited_in_place_never_duplicated(env):
    create, patch = mock_bot_pr([pull()], comments=[{"id": 99, "body": bot_prs.MARKER + "\nold"}])
    assert run(go(env, "comment_only"))[0]["action"] == "updated"
    assert patch.called and not create.called


@respx.mock
def test_mode_off_and_unknown_package_and_other_major(env):
    mock_bot_pr([pull()])
    assert run(go(env, "off")) == []
    respx.get(f"{GH}/repos/{REPO}/contents/package.json").mock(side_effect=lambda req: httpx.Response(
        200, text=json.dumps({"dependencies": {"express": "4.21.2" if req.url.params["ref"] == "basesha" else "^6.0.0"}})))
    assert run(go(env, "comment_only"))[0]["action"] == "no_run"  # Pilot has no run for express 6


@respx.mock
def test_commit_mode_waits_for_approval_then_pushes_edits(env):
    create, _ = mock_bot_pr([pull()])
    orders = "const id = Number(req.param('id'));\n"
    respx.get(url__regex=rf"{GH}/repos/{REPO}/contents/routes/.*").mock(
        side_effect=lambda req: httpx.Response(200, json=b64(orders if "orders" in str(req.url) else "// nothing to change\n")))
    put = respx.put(url__regex=rf"{GH}/repos/{REPO}/contents/routes/.*").respond(200, json={})

    first = run(go(env, "comment_and_commit"))  # the run is still awaiting approval
    assert first[0]["committed"] == [] and not put.called
    assert "only after the run is approved" in json.loads(create.calls[0].request.content)["body"]

    conn = core_db.connect(env.db)
    run_id = first[0]["run_id"]
    run_store.set_run_status(conn, run_id, "approved")
    second = run(go(env, "comment_and_commit"))
    assert second[0]["committed"] == ["routes/orders.js"]
    sent = json.loads(put.calls[0].request.content)
    assert sent["branch"] == "dependabot/npm_and_yarn/express-5.1.0" and sent["sha"] == "filesha"
    assert base64.b64decode(sent["content"]).decode() == "const id = Number(req.params.id);\n"
    assert "pushed the verified edits to this branch" in json.loads(create.calls[1].request.content)["body"]
