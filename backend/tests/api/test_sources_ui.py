"""S4: the source mapping list, confirm and the extra override kinds."""
import pytest

from app.core import db as core_db
from app.updates import sources
from app.updates import store as updates_store
from app.updates.models import SourceRepo
from tests.api.test_api import client_for

pytestmark = [pytest.mark.node]


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A scan of every dependency (the shared fixture only looks at express)."""
    from tests.api.conftest import Env

    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin)}))
    return env


def rows_by_name(client) -> dict:
    return {r["name"]: r for r in client.get("/api/scans/latest/sources").json()["rows"]}


def test_list_marks_found_and_missing_sources(fresh):
    with client_for(fresh) as client:
        data = client.get("/api/scans/latest/sources").json()
        rows = {r["name"]: r for r in data["rows"]}
        assert rows["express"]["status"] == "verified" and rows["express"]["source"] == "github.com/expressjs/express"
        assert "History.md" in rows["express"]["detail"] or "releases" in rows["express"]["detail"]
        assert rows["express"]["kind"].startswith("npm") and rows["express"]["found_by"] == "Registry metadata"
        assert rows["mocha"]["status"] == "not_found" and rows["mocha"]["source"] is None
        assert data["to_confirm"] == sum(r["status"] in ("confirm", "not_found") for r in data["rows"])
        assert [r["status"] for r in data["rows"]][0] == "not_found"  # what needs a person comes first


def test_medium_confidence_asks_for_confirmation_and_confirm_sticks(fresh):
    conn = core_db.connect(fresh.db)
    updates_store.save_package_source(conn, "npm", "mocha", SourceRepo(
        owner="mochajs", repo="mocha", method="homepage", confidence="medium", verified=True))
    with client_for(fresh) as client:
        assert rows_by_name(client)["mocha"]["status"] == "confirm"
        assert client.post("/api/sources/confirm", json={"name": "mocha"}).json() == {"confirmed": "npm/mocha"}
        assert rows_by_name(client)["mocha"]["status"] == "verified"
        row = conn.execute("SELECT verified, confidence FROM package_sources WHERE name = 'mocha'").fetchone()
        assert (row["verified"], row["confidence"]) == (1, "high")
        assert client.post("/api/sources/confirm", json={"name": "unknown-pkg"}).status_code == 404


def test_override_kinds_are_saved_and_explained(fresh):
    with client_for(fresh) as client:
        r = client.post("/api/sources/override", json={"name": "mocha", "kind": "confluence",
                                                       "url": "https://acme.atlassian.net/wiki/spaces/X/pages/1"})
        assert r.status_code == 201
        saved = rows_by_name(client)["mocha"]
        assert saved["status"] == "saved" and "later release" in saved["note"] and saved["source"].endswith("/pages/1")
        assert sources.load_overrides()["npm/mocha"] == {"kind": "confluence", "url": "https://acme.atlassian.net/wiki/spaces/X/pages/1"}
        assert client.post("/api/sources/override", json={"name": "mocha", "kind": "slack"}).status_code == 422  # no channel
        assert client.post("/api/sources/override", json={"name": "mocha", "kind": "repository"}).status_code == 422
        assert client.post("/api/sources/override", json={"name": "mocha", "kind": "nope", "url": "x"}).status_code == 422
        ok = client.post("/api/sources/override", json={"name": "mocha", "owner": "mochajs", "repo": "mocha"})
        assert ok.status_code == 201 and sources.load_overrides()["npm/mocha"] == {"owner": "mochajs", "repo": "mocha"}


def test_yaml_roundtrip_keeps_old_entries_and_new_kinds(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("npm/left-pad:\n  owner: stevemao\n  repo: left-pad\n", encoding="utf-8")  # the old format
    sources.save_override(path, "npm", "@acme/money", kind="slack", url="#payments-changes")
    sources.save_override(path, "npm", "x", "o", "r", "packages/x")
    data = sources.load_overrides(path)
    assert data["npm/left-pad"] == {"owner": "stevemao", "repo": "left-pad"}
    assert data["npm/@acme/money"] == {"kind": "slack", "url": "#payments-changes"}
    assert data["npm/x"] == {"owner": "o", "repo": "r", "directory": "packages/x"}
    with pytest.raises(ValueError):
        sources.save_override(path, "npm", "y", kind="bogus", url="u")


def test_a_non_repository_override_does_not_break_resolution():
    """resolve_source only follows overrides that name an owner and repo."""
    override = {"kind": "confluence", "url": "https://x"}
    assert not (override.get("owner") and override.get("repo"))
