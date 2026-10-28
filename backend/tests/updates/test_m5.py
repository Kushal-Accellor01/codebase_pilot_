import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.updates import store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from app.updates.models import SourceRepo
from app.updates.notes import (
    fetch_notes,
    split_changelog,
    tag_matches,
    truncate_notes,
    versions_in_range,
)
from app.updates.registry.base import PackageInfo, VersionInfo

NOTES = Path(__file__).parent.parent / "fixtures" / "notes"
GH = "https://api.github.com"


def run(coro):
    return asyncio.run(coro)


# --- split_changelog (the two required cases from the spec) ---------------------------------------------


def test_history_style_with_unreleased_heading():
    text = """unreleased
==========

  * Fix something not yet released

1.10.0 / 2020-03-20
===================

  * Add `thing` option

1.9.1 / 2018-09-10
==================

  * Fix bug
"""
    sections = split_changelog(text)
    assert set(sections) == {"1.10.0", "1.9.1"}
    assert "Add `thing` option" in sections["1.10.0"] and "Fix bug" not in sections["1.10.0"]


def test_markdown_heading_styles():
    text = """# Changelog

## [Unreleased]

- nothing

## [15.0.4] - 2024-12-01

- fix a

## v15.0.3 (2024-11-20)

- fix b

## pkg-a@2.1.0

- feat c
"""
    assert set(split_changelog(text)) == {"15.0.4", "15.0.3", "2.1.0"}


def test_prerelease_headings_and_crlf():
    sections = split_changelog("## 2.0.0-beta.1\r\n\r\n- b\r\n\r\n## 1.0.0\r\n\r\n- a\r\n")
    assert set(sections) == {"2.0.0-beta.1", "1.0.0"} and sections["1.0.0"] == "- a"


def test_real_express_history_file():
    sections = split_changelog((NOTES / "express_History.md").read_text(encoding="utf-8"))
    for version in ("5.0.0", "5.0.1", "5.1.0", "5.2.0", "5.3.0"):
        assert sections[version].strip(), version
    assert "5.0.0-beta.1" in sections and len(sections) > 100


# --- tags and ranges -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag,name,version,expected",
    [
        ("v1.2.3", "pkg", "1.2.3", True),
        ("1.2.3", "pkg", "1.2.3", True),
        ("pkg@1.2.3", "pkg", "1.2.3", True),
        ("@scope/pkg@1.2.3", "@scope/pkg", "1.2.3", True),
        ("pkg@1.2.3", "@scope/pkg", "1.2.3", True),  # monorepo tag prefix without the scope
        ("pkg-v1.2.3", "@scope/pkg", "1.2.3", True),
        ("other@1.2.3", "pkg", "1.2.3", False),
        ("v1.2.30", "pkg", "1.2.3", False),
    ],
)
def test_tag_matches(tag, name, version, expected):
    assert tag_matches(tag, name, version) is expected


def package(name: str, versions: list[str]) -> PackageInfo:
    return PackageInfo(ecosystem="npm", name=name, versions=[VersionInfo(version=v) for v in versions])


def test_versions_in_range_excludes_current_and_prereleases():
    info = package("p", ["1.0.0", "1.1.0", "1.2.0", "2.0.0-beta.1", "2.0.0", "2.1.0", "weird"])
    assert versions_in_range(info, "1.0.0", "2.0.0") == ["1.1.0", "1.2.0", "2.0.0"]
    assert versions_in_range(info, "1.0.0", "2.0.0-beta.1") == ["1.1.0", "1.2.0", "2.0.0-beta.1"]
    assert versions_in_range(info, "2.1.0", "2.1.0") == []


def test_truncate_notes():
    assert truncate_notes("short") == "short"
    cut = truncate_notes("x" * 7000)
    assert cut.endswith("[truncated]") and len(cut) < 6020 and cut.startswith("xxxx")


# --- fetch_notes -----------------------------------------------------------------------------------------------


@pytest.fixture
def conn(patched_db):
    c = store.connect(str(patched_db))
    store.migrate(c)
    return c


EXPRESS_VERSIONS = ["4.21.2", "4.22.0", "4.22.1", "4.22.2", "5.0.0", "5.0.1", "5.1.0", "5.2.0", "5.2.1"]


def express_source(**kw) -> SourceRepo:
    return SourceRepo(owner="expressjs", repo="express", default_branch="master", verified=True, **kw)


async def notes_for(conn, info, source, current, target):
    async with HttpClient(wait_scale=0) as http:
        return await fetch_notes(GitHubClient(http, "t"), conn, source, info, current, target)


def releases_fixture() -> list:
    return json.loads((NOTES / "express_releases.json").read_text(encoding="utf-8"))


@respx.mock
def test_github_releases_path(conn):
    route = respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=releases_fixture())
    result = run(notes_for(conn, package("express", EXPRESS_VERSIONS), express_source(), "4.21.2", "5.2.1"))
    assert result.versions == EXPRESS_VERSIONS[1:] and result.coverage == 1.0
    assert {n.source for n in result.notes.values()} == {"github_release"}
    # a tag spelled without 'v' ("4.22.0") and one with ("v5.0.0") both match
    assert result.notes["4.22.0"].url.startswith("https://github.com/expressjs/express/releases/")
    assert result.notes["5.0.0"].body_md.strip() and result.notes["5.0.0"].published_at
    assert route.call_count == 1


@respx.mock
def test_notes_are_cached_forever(conn):
    route = respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=releases_fixture())
    info = package("express", EXPRESS_VERSIONS)
    run(notes_for(conn, info, express_source(), "4.21.2", "5.2.1"))
    again = run(notes_for(conn, info, express_source(), "4.21.2", "5.2.1"))
    assert route.call_count == 1 and again.coverage == 1.0
    assert conn.execute("SELECT count(*) FROM release_notes").fetchone()[0] == 8
    row = conn.execute("SELECT content_sha256 FROM release_notes LIMIT 1").fetchone()
    assert len(row["content_sha256"]) == 64


@respx.mock
def test_draft_and_prerelease_skipped_for_stable_versions(conn):
    releases = [
        {"tag_name": "v1.1.0", "draft": True, "prerelease": False, "body": "draft notes", "html_url": "u1"},
        {"tag_name": "v1.2.0", "draft": False, "prerelease": True, "body": "pre notes", "html_url": "u2"},
        {"tag_name": "v1.3.0", "draft": False, "prerelease": False, "body": "real notes", "html_url": "u3",
         "published_at": "2024-01-01T00:00:00Z"},
        {"tag_name": "v1.4.0", "draft": False, "prerelease": False, "body": "", "html_url": "u4"},
    ]
    respx.get(f"{GH}/repos/o/r/releases").respond(200, json=releases)
    respx.get(url__regex=rf"{GH}/repos/o/r/contents/.*").respond(404)
    respx.get(url__regex=rf"{GH}/repos/o/r/compare/.*").respond(404)
    info = package("p", ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"])
    result = run(notes_for(conn, info, SourceRepo(owner="o", repo="r", default_branch="main"), "1.0.0", "1.4.0"))
    assert list(result.notes) == ["1.3.0"] and result.coverage == 0.25


@respx.mock
def test_changelog_file_history_style_and_learned_path(conn):
    respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=[])
    respx.get(f"{GH}/repos/expressjs/express/contents/CHANGELOG.md").respond(404)
    hist = respx.get(f"{GH}/repos/expressjs/express/contents/HISTORY.md").respond(404)
    hist2 = respx.get(f"{GH}/repos/expressjs/express/contents/History.md").respond(
        200, text=(NOTES / "express_History.md").read_text(encoding="utf-8")
    )
    info = package("express", ["4.21.2", "5.0.0", "5.0.1", "5.1.0"])
    source = express_source()
    result = run(notes_for(conn, info, source, "4.21.2", "5.1.0"))
    assert set(result.notes) == {"5.0.0", "5.0.1", "5.1.0"}
    note = result.notes["5.0.0"]
    assert note.source == "changelog_file"
    assert note.url == "https://github.com/expressjs/express/blob/master/History.md"
    assert hist.called and hist2.call_count == 1
    assert source.changelog_path == "History.md"

    # learned path is tried first on the next resolution
    store.save_package_source(conn, "npm", "express", source)
    store.remember_changelog_path(conn, "npm", "express", "History.md")
    assert store.load_package_source(conn, "npm", "express")[0].changelog_path == "History.md"


@respx.mock
def test_learned_changelog_path_is_tried_first(conn):
    respx.get(f"{GH}/repos/expressjs/express/releases").respond(200, json=[])
    learned = respx.get(f"{GH}/repos/expressjs/express/contents/History.md").respond(
        200, text=(NOTES / "express_History.md").read_text(encoding="utf-8")
    )
    other = respx.get(f"{GH}/repos/expressjs/express/contents/CHANGELOG.md").respond(404)
    run(notes_for(conn, package("express", ["4.21.2", "5.0.0"]), express_source(changelog_path="History.md"),
                  "4.21.2", "5.0.0"))
    assert learned.called and not other.called


@respx.mock
def test_changelog_in_monorepo_directory(conn):
    respx.get(f"{GH}/repos/acme/mono/releases").respond(200, json=[])
    path = "packages/pkg/CHANGELOG.md"
    route = respx.get(f"{GH}/repos/acme/mono/contents/packages%2Fpkg%2FCHANGELOG.md").respond(
        200, text="# pkg\n\n## 1.1.0\n\n- breaking: removed `foo()`\n\n## 1.0.0\n\n- initial\n"
    )
    source = SourceRepo(owner="acme", repo="mono", directory="packages/pkg", default_branch="main")
    result = run(notes_for(conn, package("@acme/pkg", ["1.0.0", "1.1.0"]), source, "1.0.0", "1.1.0"))
    assert route.called
    assert result.notes["1.1.0"].url == f"https://github.com/acme/mono/blob/main/{path}"
    assert "removed `foo()`" in result.notes["1.1.0"].body_md


@respx.mock
def test_monorepo_tag_prefix_in_releases(conn):
    releases = [
        {"tag_name": "pkg-b@1.1.0", "draft": False, "prerelease": False, "body": "wrong package", "html_url": "x"},
        {"tag_name": "pkg-a@1.1.0", "draft": False, "prerelease": False, "body": "right package", "html_url": "y"},
    ]
    respx.get(f"{GH}/repos/acme/mono/releases").respond(200, json=releases)
    source = SourceRepo(owner="acme", repo="mono", default_branch="main")
    result = run(notes_for(conn, package("@acme/pkg-a", ["1.0.0", "1.1.0"]), source, "1.0.0", "1.1.0"))
    assert result.notes["1.1.0"].body_md == "right package"


@respx.mock
def test_compare_commits_fallback_is_low_confidence(conn):
    respx.get(f"{GH}/repos/o/r/releases").respond(200, json=[])
    respx.get(url__regex=rf"{GH}/repos/o/r/contents/.*").respond(404)
    miss = respx.get(f"{GH}/repos/o/r/compare/v1.0.0...v1.2.0").respond(404)
    hit = respx.get(f"{GH}/repos/o/r/compare/1.0.0...1.2.0").respond(
        200,
        json={
            "html_url": "https://github.com/o/r/compare/1.0.0...1.2.0",
            "commits": [
                {"commit": {"message": "feat: add thing\n\nlong body"}},
                {"commit": {"message": "fix: crash"}},
            ],
        },
    )
    source = SourceRepo(owner="o", repo="r", default_branch="main")
    result = run(notes_for(conn, package("p", ["1.0.0", "1.1.0", "1.2.0"]), source, "1.0.0", "1.2.0"))
    assert miss.called and hit.called
    assert list(result.notes) == ["1.2.0"] and result.coverage == pytest.approx(0.5)
    note = result.notes["1.2.0"]
    assert note.source == "compare_commits" and "- feat: add thing" in note.body_md and "long body" not in note.body_md


@respx.mock
def test_compare_not_used_when_any_notes_exist(conn):
    respx.get(f"{GH}/repos/o/r/releases").respond(
        200, json=[{"tag_name": "v1.1.0", "draft": False, "prerelease": False, "body": "n", "html_url": "u"}]
    )
    respx.get(url__regex=rf"{GH}/repos/o/r/contents/.*").respond(404)
    compare = respx.get(url__regex=rf"{GH}/repos/o/r/compare/.*").respond(200, json={})
    source = SourceRepo(owner="o", repo="r", default_branch="main")
    run(notes_for(conn, package("p", ["1.0.0", "1.1.0", "1.2.0"]), source, "1.0.0", "1.2.0"))
    assert not compare.called


@respx.mock
def test_nothing_found_means_unavailable(conn):
    respx.get(f"{GH}/repos/o/r/releases").respond(200, json=[])
    respx.get(url__regex=rf"{GH}/repos/o/r/contents/.*").respond(404)
    respx.get(url__regex=rf"{GH}/repos/o/r/compare/.*").respond(404)
    source = SourceRepo(owner="o", repo="r", default_branch="main")
    result = run(notes_for(conn, package("p", ["1.0.0", "1.1.0"]), source, "1.0.0", "1.1.0"))
    assert not result.available and result.coverage == 0.0 and result.versions == ["1.1.0"]


def test_unknown_source_makes_no_requests(conn):
    source = SourceRepo(owner="", repo="", status="unknown")
    result = run(notes_for(conn, package("p", ["1.0.0", "1.1.0"]), source, "1.0.0", "1.1.0"))
    assert not result.available


def test_empty_range(conn):
    result = run(notes_for(conn, package("p", ["1.0.0"]), express_source(), "1.0.0", "1.0.0"))
    assert result.versions == [] and result.coverage == 0.0


@respx.mock
def test_releases_pagination_stops_at_current_version(conn):
    page1 = [{"tag_name": f"v1.{i}.0", "draft": False, "prerelease": False, "body": "n", "html_url": "u"}
             for i in range(150, 50, -1)]
    page2 = [{"tag_name": "v1.0.0", "draft": False, "prerelease": False, "body": "old", "html_url": "u"}]
    route = respx.get(f"{GH}/repos/o/r/releases").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    versions = ["1.0.0"] + [f"1.{i}.0" for i in range(1, 151)]
    source = SourceRepo(owner="o", repo="r", default_branch="main")
    # only 1.149.0 and 1.150.0 are missing in practice: ask for a small range at the top
    result = run(notes_for(conn, package("p", versions), source, "1.147.0", "1.150.0"))
    assert set(result.notes) == {"1.148.0", "1.149.0", "1.150.0"} and route.call_count == 1
