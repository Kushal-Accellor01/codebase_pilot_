import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.updates import sources, store
from app.updates.github import GitHubClient, GitHubRateLimited
from app.updates.http import HttpClient
from app.updates.registry.base import PackageInfo
from app.updates.sources import (
    get_source,
    homepage_repo,
    load_overrides,
    parse_github_repo,
    resolve_source,
    save_override,
)

FIX = Path(__file__).parent.parent / "fixtures" / "sources"
GH = "https://api.github.com"
DEPS = "https://api.deps.dev/v3/systems/npm/packages"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# --- parse_github_repo (inputs from the spec) -----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("git+https://github.com/expressjs/morgan.git", ("expressjs", "morgan", None)),
        ("git://github.com/jshttp/http-errors.git", ("jshttp", "http-errors", None)),
        ("git@github.com:pillarjs/router.git", ("pillarjs", "router", None)),
        ("https://github.com/markedjs/marked", ("markedjs", "marked", None)),
        ("github:visionmedia/debug", ("visionmedia", "debug", None)),
        ("expressjs/session", ("expressjs", "session", None)),
        ("https://github.com/foo/bar.js.git#main", ("foo", "bar.js", None)),
        ({"type": "git", "url": "git+https://github.com/o/r.git", "directory": "packages/a"}, ("o", "r", "packages/a")),
        ("https://gitlab.com/foo/bar.git", None),
        (None, None),
        ({"type": "git"}, None),
        (42, None),
    ],
)
def test_parse_github_repo(value, expected):
    assert parse_github_repo(value) == expected


def test_homepage_repo():
    assert homepage_repo("https://github.com/o/r#readme") == ("o", "r")
    assert homepage_repo("https://github.com/o/r.git") == ("o", "r")
    assert homepage_repo("https://example.com/o/r") is None
    assert homepage_repo(None) is None


# --- override file ------------------------------------------------------------------------------------


def test_override_yaml_roundtrip(tmp_path):
    path = tmp_path / "sources.yaml"
    assert load_overrides(path) == {}
    path.write_text("{}\n", encoding="utf-8")
    assert load_overrides(path) == {}
    save_override(path, "npm", "@scope/pkg", "acme", "mono", "packages/pkg")
    save_override(path, "npm", "left-pad", "stevemao", "left-pad")
    loaded = load_overrides(path)
    assert loaded["npm/@scope/pkg"] == {"owner": "acme", "repo": "mono", "directory": "packages/pkg"}
    assert loaded["npm/left-pad"] == {"owner": "stevemao", "repo": "left-pad"}


def test_override_yaml_must_be_a_mapping(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_overrides(path)


def test_shipped_override_file_is_valid():
    assert load_overrides(sources.DEFAULT_OVERRIDES_PATH) == {}


# --- chain --------------------------------------------------------------------------------------------


def morgan_info(**kw) -> PackageInfo:
    base = dict(
        ecosystem="npm",
        name="morgan",
        latest_version="1.12.1",
        repository={"type": "git", "url": "git+https://github.com/expressjs/morgan.git"},
    )
    return PackageInfo(**{**base, **kw})


def mock_github_morgan(owner="expressjs", repo="morgan", name_in_package_json=None):
    pkg = fixture("github_package_json_morgan.json")
    if name_in_package_json:
        pkg = json.dumps({**json.loads(pkg), "name": name_in_package_json})
    return (
        respx.get(f"{GH}/repos/{owner}/{repo}/contents/package.json").respond(200, text=pkg),
        respx.get(f"{GH}/repos/{owner}/{repo}").respond(200, text=fixture("github_repo_morgan.json")),
    )


async def resolve(info, overrides=None, token="t"):
    async with HttpClient(wait_scale=0) as http:
        gh = GitHubClient(http, token)
        return await resolve_source(gh, http, info, "1.12.1", overrides if overrides is not None else {})


@respx.mock
def test_level_registry_repository():
    pkg_route, repo_route = mock_github_morgan()
    src = run(resolve(morgan_info()))
    assert (src.owner, src.repo, src.method, src.confidence) == ("expressjs", "morgan", "registry", "high")
    assert src.verified and not src.archived and src.default_branch == "master"
    assert src.slug == "github.com/expressjs/morgan" and src.status == "resolved"
    assert pkg_route.calls[0].request.headers["accept"] == "application/vnd.github.raw"
    assert pkg_route.calls[0].request.headers["authorization"] == "Bearer t"


@respx.mock
def test_level_override_wins_and_carries_changelog_path():
    respx.get(f"{GH}/repos/acme/fork/contents/package.json").respond(200, text=json.dumps({"name": "morgan"}))
    respx.get(f"{GH}/repos/acme/fork").respond(
        200, json={"name": "fork", "owner": {"login": "acme"}, "default_branch": "main", "archived": False}
    )
    src = run(resolve(
        morgan_info(),
        {"npm/morgan": {"owner": "acme", "repo": "fork", "changelog_path": "docs/HISTORY.md"}},
    ))
    assert (src.owner, src.method, src.changelog_path) == ("acme", "override", "docs/HISTORY.md")


@respx.mock
def test_failed_override_falls_through_to_registry():
    respx.get(f"{GH}/repos/acme/fork/contents/package.json").respond(404)
    mock_github_morgan()
    src = run(resolve(morgan_info(), {"npm/morgan": {"owner": "acme", "repo": "fork"}}))
    assert src.method == "registry"


@respx.mock
def test_level_deps_dev():
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(200, text=fixture("depsdev_morgan.json"))
    mock_github_morgan()
    src = run(resolve(morgan_info(repository=None)))
    assert (src.owner, src.repo, src.method, src.confidence) == ("expressjs", "morgan", "deps_dev", "high")


@respx.mock
def test_deps_dev_encodes_scoped_names():
    route = respx.get(f"{DEPS}/@scope%2Fpkg/versions/1.0.0").respond(404)

    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await sources.deps_dev_repo(http, "@scope/pkg", "1.0.0")

    assert run(go()) is None and route.called


@respx.mock
def test_level_homepage_is_medium_confidence():
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(404)
    mock_github_morgan()
    src = run(resolve(morgan_info(repository=None, homepage="https://github.com/expressjs/morgan#readme")))
    assert (src.method, src.confidence) == ("homepage", "medium")


@respx.mock
def test_verification_rejects_repo_with_different_package_name():
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(404)
    mock_github_morgan(name_in_package_json="not-morgan")
    src = run(resolve(morgan_info()))
    assert src.status == "unknown" and src.method == "manual" and not src.verified


@respx.mock
def test_unknown_when_nothing_found():
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(200, json={"relatedProjects": []})
    src = run(resolve(morgan_info(repository="https://gitlab.com/o/r", homepage="https://example.com")))
    assert src.status == "unknown" and src.owner == ""


@respx.mock
def test_monorepo_directory_is_used_for_verification():
    pkg = respx.get(f"{GH}/repos/acme/mono/contents/packages/pkg/package.json").respond(
        200, text=json.dumps({"name": "@acme/pkg"})
    )
    respx.get(f"{GH}/repos/acme/mono").respond(200, json={"name": "mono", "owner": {"login": "acme"},
                                                           "default_branch": "main", "archived": False})
    info = PackageInfo(
        ecosystem="npm", name="@acme/pkg",
        repository={"url": "git+https://github.com/acme/mono.git", "directory": "packages/pkg"},
    )
    src = run(resolve(info))
    assert src.directory == "packages/pkg" and src.default_branch == "main" and pkg.called


@respx.mock
def test_archived_repo_is_flagged():
    respx.get(f"{GH}/repos/expressjs/morgan/contents/package.json").respond(
        200, text=fixture("github_package_json_morgan.json")
    )
    repo = json.loads(fixture("github_repo_morgan.json"))
    repo["archived"] = True
    respx.get(f"{GH}/repos/expressjs/morgan").respond(200, json=repo)
    assert run(resolve(morgan_info())).archived is True


@respx.mock
def test_same_candidate_is_not_verified_twice():
    pkg, _ = mock_github_morgan()
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(200, text=fixture("depsdev_morgan.json"))
    # registry candidate fails (wrong name), deps.dev proposes the same repo: no second verification
    pkg.respond(200, text=json.dumps({"name": "other"}))
    src = run(resolve(morgan_info(homepage="https://github.com/expressjs/morgan")))
    assert src.status == "unknown" and pkg.call_count == 1


# --- cache --------------------------------------------------------------------------------------------


@pytest.fixture
def conn(patched_db):
    c = store.connect(str(patched_db))
    store.migrate(c)
    return c


async def cached_source(conn, info, **kw):
    async with HttpClient(wait_scale=0) as http:
        return await get_source(GitHubClient(http, "t"), http, conn, info, "1.12.1", overrides={}, **kw)


@respx.mock
def test_source_cached_for_30_days(conn):
    pkg, _ = mock_github_morgan()
    run(cached_source(conn, morgan_info()))
    again = run(cached_source(conn, morgan_info()))
    assert pkg.call_count == 1 and again.owner == "expressjs" and again.verified
    conn.execute("UPDATE package_sources SET resolved_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    run(cached_source(conn, morgan_info()))
    assert pkg.call_count == 2


@respx.mock
def test_unknown_is_cached_briefly_and_refresh_forces(conn):
    respx.get(f"{DEPS}/morgan/versions/1.12.1").respond(404)
    info = morgan_info(repository=None)
    run(cached_source(conn, info))
    row = conn.execute("SELECT status FROM package_sources").fetchone()
    assert row["status"] == "unknown"
    calls = respx.calls.call_count
    run(cached_source(conn, info))
    assert respx.calls.call_count == calls  # served from cache
    run(cached_source(conn, info, refresh=True))
    assert respx.calls.call_count > calls


@respx.mock
def test_learned_changelog_path_survives_re_resolution(conn):
    mock_github_morgan()
    run(cached_source(conn, morgan_info()))
    store.remember_changelog_path(conn, "npm", "morgan", "HISTORY.md")
    run(cached_source(conn, morgan_info(), refresh=True))
    assert store.load_package_source(conn, "npm", "morgan")[0].changelog_path == "HISTORY.md"


# --- GitHub client: rate limits ----------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


async def gh_get(clock, path="repos/o/r", token="t", max_wait=120.0, calls=1):
    async with HttpClient(wait_scale=0) as http:
        gh = GitHubClient(http, token, sleeper=clock.sleep, clock=clock.time, max_wait_s=max_wait)
        return [await gh.get_json(path) for _ in range(calls)]


@respx.mock
def test_pauses_when_remaining_is_low_then_continues():
    clock = Clock()
    respx.get(f"{GH}/repos/o/r").respond(
        200, json={"ok": True}, headers={"x-ratelimit-remaining": "10", "x-ratelimit-reset": "1030"}
    )
    run(gh_get(clock, calls=2))
    assert clock.slept == [pytest.approx(31.0)]


@respx.mock
def test_raises_when_reset_is_too_far_away():
    clock = Clock()
    respx.get(f"{GH}/repos/o/r").respond(
        200, json={}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999"}
    )
    with pytest.raises(GitHubRateLimited):
        run(gh_get(clock, calls=2))


@respx.mock
def test_secondary_limit_waits_retry_after_and_retries():
    clock = Clock()
    route = respx.get(f"{GH}/repos/o/r").mock(
        side_effect=[httpx.Response(403, headers={"retry-after": "7"}), httpx.Response(200, json={"ok": 1})]
    )
    assert run(gh_get(clock)) == [{"ok": 1}]
    assert clock.slept == [7.0] and route.call_count == 2


@respx.mock
def test_plain_403_and_404_return_none():
    clock = Clock()
    respx.get(f"{GH}/repos/o/r").respond(403)
    respx.get(f"{GH}/repos/o/missing").respond(404)
    assert run(gh_get(clock)) == [None]
    assert run(gh_get(clock, path="repos/o/missing")) == [None]


@respx.mock
def test_no_token_means_no_authorization_header():
    clock = Clock()
    route = respx.get(f"{GH}/repos/o/r").respond(200, json={})
    run(gh_get(clock, token=""))
    assert "authorization" not in route.calls[0].request.headers
