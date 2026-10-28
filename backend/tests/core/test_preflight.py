import asyncio

import httpx
import respx

from app.core import preflight
from app.core.config import Settings
from app.updates.http import HttpClient

GH = "https://api.github.com"


def settings(tmp_path, **kw) -> Settings:
    base = dict(_env_file=None, codebase_pilot_db=str(tmp_path / "db" / "p.db"),
                codebase_pilot_workspace=str(tmp_path / "ws"), reports_dir=str(tmp_path / "rep"), llm_mode="replay",
                npm_registry_url="https://registry.example.test")
    return Settings(**{**base, **kw})


def run(s, **kw):
    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await preflight.run_checks(s, http=http, **kw)

    return {c.name: c for c in asyncio.run(go())}


def mock_all(push=True, archived=False):
    respx.get(f"{GH}/user").respond(200, json={"login": "kushal"}, headers={"x-oauth-scopes": "repo"})
    respx.get(f"{GH}/repos/acme/shop").respond(
        200, json={"default_branch": "main", "archived": archived, "permissions": {"push": push}})
    respx.get(f"{GH}/rate_limit").respond(200, json={"resources": {"core": {"remaining": 4900, "limit": 5000}}})
    respx.get("https://registry.example.test/express").respond(200, json={})
    respx.post("https://api.osv.dev/v1/query").respond(200, json={})


@respx.mock
def test_everything_configured_is_ready(tmp_path):
    mock_all()
    r = run(settings(tmp_path, github_token="ghp_SECRET123", exec_allowed_repos="acme/shop"))
    assert r["GitHub token"].status == "ok" and "kushal" in r["GitHub token"].detail
    assert r["repo acme/shop"].status == "ok" and "can push" in r["repo acme/shop"].detail
    assert r["npm registry"].status == "ok" and r["OSV advisories"].status == "ok"
    assert r["EXEC_ALLOWED_REPOS"].status == "ok" and r["writable: workspace"].status == "ok"
    assert "ghp_SECRET123" not in preflight.render(list(r.values()))  # secrets never reach the output


@respx.mock
def test_each_misconfiguration_names_its_fix(tmp_path):
    respx.get(f"{GH}/user").respond(401, json={})
    respx.get("https://registry.example.test/express").respond(401, json={})
    respx.post("https://api.osv.dev/v1/query").mock(side_effect=httpx.ConnectError("offline"))
    r = run(settings(tmp_path, github_token="bad", llm_mode="live", npm_registry_token=None))
    assert r["GitHub token"].status == "fail" and "GITHUB_TOKEN" in r["GitHub token"].fix
    assert r["model settings"].status == "fail" and "AZURE_OPENAI_ENDPOINT" in r["model settings"].detail
    assert r["EXEC_ALLOWED_REPOS"].status == "fail"
    assert r["npm registry"].status == "fail" and "NPM_REGISTRY_TOKEN" in r["npm registry"].fix
    assert r["OSV advisories"].status == "warn"
    assert r["API protection"].status == "warn" and r["webhook"].status == "skip"


@respx.mock
def test_read_only_token_and_missing_repo_are_reported(tmp_path):
    mock_all(push=False)
    respx.get(f"{GH}/repos/acme/gone").respond(404, json={})
    r = run(settings(tmp_path, github_token="ghp_SECRET123", exec_allowed_repos="acme/shop,acme/gone,C:/local/repo"))
    assert r["repo acme/shop"].status == "warn" and "write" in r["repo acme/shop"].fix
    assert r["repo acme/gone"].status == "fail"
    assert r["repo C:/local/repo"].status == "skip"


@respx.mock
def test_repo_option_checks_the_allowlist(tmp_path):
    mock_all()
    r = run(settings(tmp_path, github_token="ghp_SECRET123", exec_allowed_repos="acme/shop"), repo="https://github.com/other/thing")
    assert r["repo allowlist"].status == "fail" and "other/thing" in r["repo allowlist"].fix
    ok = run(settings(tmp_path, github_token="ghp_SECRET123", exec_allowed_repos="acme/shop"), repo="https://github.com/acme/shop")
    assert ok["repo allowlist"].status == "ok"


def test_exit_code_follows_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEBASE_PILOT_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("CODEBASE_PILOT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "rep"))
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.delenv("EXEC_ALLOWED_REPOS", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with respx.mock:
        respx.get("https://registry.npmjs.org/express").respond(200, json={})
        respx.post("https://api.osv.dev/v1/query").respond(200, json={})
        code = preflight.main(["--no-model"])
    assert code == 1 and "Not ready" in capsys.readouterr().out
