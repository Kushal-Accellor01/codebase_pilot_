import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.updates import store
from app.updates.http import HttpClient, HttpError
from app.updates.registry.cache import get_package
from app.updates.registry.npm import NpmRegistry, package_url, parse_package

FIXTURES = Path(__file__).parent.parent / "fixtures" / "npm"
BASE = "https://registry.npmjs.org"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / (name.replace("/", "__") + ".json")).read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


async def fetch(name: str, **kw):
    async with HttpClient(wait_scale=0) as http:
        return await NpmRegistry(http, BASE).fetch_package(name, **kw)


def test_scoped_name_encoding():
    assert package_url(BASE, "@scope/pkg") == f"{BASE}/@scope%2Fpkg"
    assert package_url(BASE + "/", "morgan") == f"{BASE}/morgan"


def test_parse_morgan_fixture():
    doc = fixture("morgan")
    info = parse_package("morgan", doc)
    assert info.latest_version == doc["dist-tags"]["latest"] == "1.12.1"
    assert len(info.versions) == len(doc["versions"])
    v = next(v for v in info.versions if v.version == "1.10.0")
    assert v.published_at == doc["time"]["1.10.0"]
    assert not v.is_prerelease
    assert info.repository["url"].endswith("expressjs/morgan.git")
    assert info.registry_modified == doc["time"]["modified"]
    assert not info.is_deprecated
    # 'created' / 'modified' are never treated as versions
    assert {"created", "modified"}.isdisjoint(x.version for x in info.versions)
    assert any(x.engines.get("node") for x in info.versions)


def test_parse_deprecated_package():
    info = parse_package("left-pad", fixture("left-pad"))
    assert info.is_deprecated
    assert "padStart" in info.deprecation_message
    assert all(v.deprecation_message for v in info.versions if v.version == info.latest_version)


def test_parse_prerelease_and_dist_tags():
    doc = {
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {}, "2.0.0-beta.1": {"peerDependencies": {"x": "^1"}}},
        "time": {"created": "a", "modified": "b", "1.0.0": "c", "2.0.0-beta.1": "d"},
    }
    info = parse_package("p", doc)
    pre = next(v for v in info.versions if v.version == "2.0.0-beta.1")
    assert pre.is_prerelease and pre.peer_dependencies == {"x": "^1"}
    assert info.versions[0].engines == {}


def test_parse_scoped_fixture():
    info = parse_package("@tootallnate/once", fixture("@tootallnate/once"))
    assert info.name == "@tootallnate/once"
    assert info.latest_version == "3.0.1"


def test_parse_string_repository():
    doc = {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {}}, "repository": "expressjs/session"}
    assert parse_package("p", doc).repository == "expressjs/session"


@respx.mock
def test_fetch_ok_sends_full_document_headers_and_etag():
    route = respx.get(f"{BASE}/morgan").respond(200, json=fixture("morgan"), headers={"etag": 'W/"abc"'})
    info = run(fetch("morgan"))
    assert info.status == "ok" and info.etag == 'W/"abc"'
    headers = route.calls[0].request.headers
    assert headers["accept"] == "application/json"  # not the abbreviated install-v1 form
    assert "if-none-match" not in headers


@respx.mock
def test_fetch_scoped_uses_encoded_url():
    route = respx.get(f"{BASE}/@tootallnate%2Fonce").respond(200, json=fixture("@tootallnate/once"))
    info = run(fetch("@tootallnate/once"))
    assert info.status == "ok"
    assert route.called


@respx.mock
def test_fetch_404_is_not_found():
    respx.get(f"{BASE}/nope").respond(404, json={"error": "Not found"})
    info = run(fetch("nope"))
    assert info.status == "not_found" and info.versions == []


@respx.mock
def test_fetch_304_not_modified():
    route = respx.get(f"{BASE}/morgan").respond(304)
    info = run(fetch("morgan", etag='W/"abc"'))
    assert info.status == "not_modified"
    assert route.calls[0].request.headers["if-none-match"] == 'W/"abc"'


@respx.mock
def test_retries_on_503_then_succeeds():
    route = respx.get(f"{BASE}/morgan").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=fixture("morgan"))]
    )
    assert run(fetch("morgan")).status == "ok"
    assert route.call_count == 2


@respx.mock
def test_retries_on_429_honouring_retry_after():
    route = respx.get(f"{BASE}/morgan").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=fixture("morgan")),
        ]
    )
    assert run(fetch("morgan")).status == "ok"
    assert route.call_count == 2


@respx.mock
def test_retries_on_timeout_then_succeeds():
    route = respx.get(f"{BASE}/morgan").mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json=fixture("morgan"))]
    )
    assert run(fetch("morgan")).status == "ok"
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_three_attempts():
    route = respx.get(f"{BASE}/morgan").respond(500)
    with pytest.raises(HttpError) as err:
        run(fetch("morgan"))
    assert err.value.status_code == 500
    assert route.call_count == 3


@respx.mock
def test_404_is_not_retried():
    route = respx.get(f"{BASE}/nope").respond(404)
    run(fetch("nope"))
    assert route.call_count == 1


# --- persistence and cache -------------------------------------------------------------

@pytest.fixture
def conn(patched_db):
    c = store.connect(str(patched_db))
    store.migrate(c)
    return c


def test_save_and_load_roundtrip(conn):
    info = parse_package("morgan", fixture("morgan"), etag="e1")
    store.save_registry_package(conn, info)
    store.save_registry_package(conn, info)  # idempotent
    loaded, fetched_at = store.load_registry_package(conn, "npm", "morgan")
    assert loaded.latest_version == "1.12.1" and loaded.etag == "e1"
    assert len(loaded.versions) == len(info.versions)
    assert loaded.repository == info.repository
    assert fetched_at.tzinfo is not None


def test_save_keeps_repository_directory(conn):
    repo = {"type": "git", "url": "git+https://github.com/o/r.git", "directory": "pkgs/a"}
    doc = {"dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"repository": repo}}}
    store.save_registry_package(conn, parse_package("@o/a", doc))
    loaded, _ = store.load_registry_package(conn, "npm", "@o/a")
    assert loaded.repository["directory"] == "pkgs/a"


def test_failure_row(conn):
    store.save_registry_failure(conn, "npm", "x", "boom")
    row = conn.execute("SELECT status, error FROM registry_packages WHERE name='x'").fetchone()
    assert (row["status"], row["error"]) == ("error", "boom")


class FakeAdapter:
    ecosystem = "npm"

    def __init__(self, doc=None, status="ok"):
        self.calls: list[str | None] = []
        self.doc = doc
        self.status = status

    async def fetch_package(self, name, *, etag=None):
        self.calls.append(etag)
        if self.status == "not_modified":
            from app.updates.registry.base import PackageInfo

            return PackageInfo(ecosystem="npm", name=name, status="not_modified", etag=etag)
        return parse_package(name, self.doc, etag="e1")


def test_cache_hit_skips_fetch(conn):
    adapter = FakeAdapter(fixture("morgan"))
    run(get_package(adapter, conn, "morgan"))
    run(get_package(adapter, conn, "morgan"))
    assert len(adapter.calls) == 1


def test_cache_refresh_forces_fetch_with_etag(conn):
    adapter = FakeAdapter(fixture("morgan"))
    run(get_package(adapter, conn, "morgan"))
    run(get_package(adapter, conn, "morgan", refresh=True))
    assert adapter.calls == [None, "e1"]


def test_stale_cache_uses_304(conn):
    run(get_package(FakeAdapter(fixture("morgan")), conn, "morgan"))
    conn.execute("UPDATE registry_packages SET fetched_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    adapter = FakeAdapter(status="not_modified")
    info = run(get_package(adapter, conn, "morgan"))
    assert adapter.calls == ["e1"]
    assert info.latest_version == "1.12.1"
    assert store.load_registry_package(conn, "npm", "morgan")[1].year >= 2026


def test_not_found_is_cached(conn):
    from app.updates.registry.base import PackageInfo

    class Missing(FakeAdapter):
        async def fetch_package(self, name, *, etag=None):
            self.calls.append(etag)
            return PackageInfo(ecosystem="npm", name=name, status="not_found")

    adapter = Missing()
    assert run(get_package(adapter, conn, "ghost")).status == "not_found"
    assert run(get_package(adapter, conn, "ghost")).status == "not_found"
    assert len(adapter.calls) == 1


# --- private registry auth (IMP-4) ----------------------------------------------------------

PRIVATE = "https://npm.example.com/feed"


async def fetch_private(name: str, token: str | None):
    async with HttpClient(wait_scale=0) as http:
        return await NpmRegistry(http, PRIVATE, token=token).fetch_package(name)


@respx.mock
def test_token_sent_to_registry_host():
    route = respx.get(f"{PRIVATE}/morgan").respond(200, json=fixture("morgan"))
    run(fetch_private("morgan", "s3cret"))
    assert route.calls[0].request.headers["authorization"] == "Bearer s3cret"


@respx.mock
def test_no_authorization_header_without_token(monkeypatch):
    monkeypatch.delenv("NPM_REGISTRY_TOKEN", raising=False)
    route = respx.get(f"{PRIVATE}/morgan").respond(200, json=fixture("morgan"))
    run(fetch_private("morgan", None))
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_token_read_from_environment(monkeypatch):
    monkeypatch.setenv("NPM_REGISTRY_TOKEN", "from-env")
    route = respx.get(f"{PRIVATE}/morgan").respond(200, json=fixture("morgan"))
    run(fetch_private("morgan", None))
    assert route.calls[0].request.headers["authorization"] == "Bearer from-env"


@respx.mock
def test_token_not_forwarded_to_other_host_on_redirect():
    respx.get(f"{PRIVATE}/morgan").respond(302, headers={"location": "https://cdn.example.org/morgan"})
    other = respx.get("https://cdn.example.org/morgan").respond(200, json=fixture("morgan"))
    run(fetch_private("morgan", "s3cret"))
    assert "authorization" not in other.calls[0].request.headers


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_auth_failure_recorded_without_retry_or_token(status, conn, caplog):
    from app.updates.registry.base import RegistryAuthError

    route = respx.get(f"{PRIVATE}/morgan").respond(status)
    with caplog.at_level("DEBUG"), pytest.raises(RegistryAuthError) as err:
        run(fetch_private("morgan", "s3cret"))
    assert route.call_count == 1
    assert str(err.value) == f"registry rejected credentials (HTTP {status})"
    assert "s3cret" not in caplog.text and "s3cret" not in str(err.value)
    store.save_registry_failure(conn, "npm", "morgan", str(err.value))
    row = conn.execute("SELECT status, error FROM registry_packages WHERE name='morgan'").fetchone()
    assert (row["status"], row["error"]) == ("error", f"registry rejected credentials (HTTP {status})")
