"""Fetch release notes for every version in (current, target] (S2 5.7).

Per version, newest first, the first source that has it wins:
  1. GitHub Releases   2. changelog file on the default branch   3. compare commits (whole-range fallback)
"""
import logging
import re
import sqlite3
from functools import cmp_to_key
from urllib.parse import quote

from app.updates import store
from app.updates.github import GitHubClient
from app.updates.models import NotesResult, ReleaseNote, SourceRepo
from app.updates.registry.base import PackageInfo
from app.updates.versions import clean_version, compare_versions

logger = logging.getLogger(__name__)

CHANGELOG_NAMES = (
    "CHANGELOG.md", "HISTORY.md", "History.md", "CHANGES.md", "changelog.md", "RELEASES.md", "NEWS.md",
)
MAX_RELEASE_PAGES = 5  # 500 releases is more than any upgrade range needs
MAX_COMPARE_COMMITS = 100
NOTE_CHAR_LIMIT = 6000

# --- changelog parsing (S2 7.1) ---------------------------------------------------------------------

VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?![\w.])")
ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")


def split_changelog(text: str) -> dict[str, str]:
    """Return {version: section_markdown}. Handles '## [1.2.3] - date', '## v1.2.3',
    'pkg@1.2.3' and the HISTORY.md style '1.2.3 / 2024-01-01' underlined with '==='."""
    lines = text.replace("\r\n", "\n").split("\n")
    heads = []  # (heading_line, body_start, version)
    i = 0
    while i < len(lines):
        line = lines[i]
        m = ATX_RE.match(line)
        if m:
            v = VERSION_RE.search(m.group(1))
            if v:
                heads.append((i, i + 1, v.group(1)))
        elif (i + 1 < len(lines) and line.strip()
              and SETEXT_RE.match(lines[i + 1]) and len(lines[i + 1].strip()) >= 3):
            v = VERSION_RE.search(line)
            if v:
                heads.append((i, i + 2, v.group(1)))
                i += 1
        i += 1
    out: dict[str, str] = {}
    for n, (start, body, ver) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        out.setdefault(ver, "\n".join(lines[body:end]).strip())
    return out


# --- tags (S2 7.2) --------------------------------------------------------------------------------------


def tag_matches(tag: str, name: str, version: str) -> bool:
    short = name.split("/")[-1]
    return tag in {version, f"v{version}", f"{name}@{version}", f"{short}@{version}",
                   f"{short}-v{version}", f"{short}-{version}", f"{name}@v{version}"}


def tag_candidates(name: str, version: str) -> list[str]:
    """Tag spellings to try for compare, most common first."""
    short = name.split("/")[-1]
    return [f"v{version}", version, f"{name}@{version}", f"{short}@{version}", f"{short}-v{version}"]


# --- helpers -----------------------------------------------------------------------------------------------


def _core(version: str) -> str:
    return version.split("-", 1)[0]


def versions_in_range(info: PackageInfo, current: str, target: str) -> list[str]:
    """Versions v with current < v <= target, oldest first.

    Pre-releases are left out unless the target is one, with one exception: the pre-releases of a *major*
    release (X.0.0-alpha.1, -beta.2 ...) that falls inside the range. Maintainers often list the removals of
    a major only in those sections (Express 5 does), and the stable X.0.0 notes just summarise them.
    """
    target_is_pre = "-" in target
    found = set()
    for v in info.versions:
        clean = clean_version(v.version)
        if clean is None:
            continue
        if "-" in clean and not target_is_pre:
            core = _core(clean)
            is_major_in_range = compare_versions(core, current) > 0 and compare_versions(core, target) <= 0
            if not (core.endswith(".0.0") and is_major_in_range):
                continue
        if compare_versions(clean, current) > 0 and compare_versions(clean, target) <= 0:
            found.add(clean)
    return sorted(found, key=cmp_to_key(compare_versions))


def truncate_notes(text: str, limit: int = NOTE_CHAR_LIMIT) -> str:
    """Keep the start of long notes (breaking changes are listed first) and mark the cut."""
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[truncated]"


def _blob_url(source: SourceRepo, path: str) -> str:
    branch = source.default_branch or "HEAD"
    return f"https://github.com/{source.owner}/{source.repo}/blob/{branch}/{path}"


def _repo_path(source: SourceRepo, suffix: str) -> str:
    return f"repos/{source.owner}/{source.repo}/{suffix}"


# --- sources -----------------------------------------------------------------------------------------------


async def _from_github_releases(
    gh: GitHubClient, source: SourceRepo, name: str, missing: set[str], current: str
) -> dict[str, ReleaseNote]:
    found: dict[str, ReleaseNote] = {}
    wanted_pre = {v for v in missing if "-" in v}
    for page in range(1, MAX_RELEASE_PAGES + 1):
        releases = await gh.get_json(
            _repo_path(source, "releases"), params={"per_page": 100, "page": page}
        )
        if not releases:
            break
        oldest_reached = False
        for rel in releases:
            tag = rel.get("tag_name") or ""
            if rel.get("draft"):
                continue
            for version in list(missing - set(found)):
                if not tag_matches(tag, name, version):
                    continue
                if rel.get("prerelease") and version not in wanted_pre:
                    continue
                body = (rel.get("body") or "").strip()
                if body:
                    found[version] = ReleaseNote(
                        version=version, source="github_release", url=rel["html_url"],
                        published_at=rel.get("published_at"), body_md=body,
                    )
            tag_version = clean_version(tag.split("@")[-1]) if tag else None
            if tag_version and compare_versions(tag_version, current) <= 0:
                oldest_reached = True
        if oldest_reached or set(found) >= missing or len(releases) < 100:
            break
    return found


async def _from_changelog_files(
    gh: GitHubClient, source: SourceRepo, missing: set[str], conn: sqlite3.Connection, ecosystem: str, name: str
) -> dict[str, ReleaseNote]:
    found: dict[str, ReleaseNote] = {}
    prefix = f"{source.directory.strip('/')}/" if source.directory else ""
    names = list(dict.fromkeys([source.changelog_path, *(prefix + n for n in CHANGELOG_NAMES)]))
    for path in filter(None, names):
        params = {"ref": source.default_branch} if source.default_branch else None
        text = await gh.get_text(_repo_path(source, f"contents/{quote(path)}"), params=params)
        if text is None:
            continue
        sections = split_changelog(text)
        hits = {v: body for v, body in sections.items() if v in missing and v not in found and body.strip()}
        if not hits:
            continue
        for version, body in hits.items():
            found[version] = ReleaseNote(
                version=version, source="changelog_file", url=_blob_url(source, path), body_md=body.strip()
            )
        if source.changelog_path != path:
            store.remember_changelog_path(conn, ecosystem, name, path)
            source.changelog_path = path
        if set(found) >= missing:
            break
    return found


async def _from_compare(
    gh: GitHubClient, source: SourceRepo, name: str, current: str, target: str
) -> ReleaseNote | None:
    for old_tag, new_tag in zip(tag_candidates(name, current), tag_candidates(name, target)):
        data = await gh.get_json(_repo_path(source, f"compare/{quote(old_tag, safe='')}...{quote(new_tag, safe='')}"))
        if not data:
            continue
        lines = []
        for commit in (data.get("commits") or [])[:MAX_COMPARE_COMMITS]:
            first_line = (commit.get("commit", {}).get("message") or "").split("\n", 1)[0].strip()
            if first_line:
                lines.append(f"- {first_line}")
        if lines:
            fallback_url = f"https://github.com/{source.owner}/{source.repo}/compare/{old_tag}...{new_tag}"
            return ReleaseNote(
                version=target, source="compare_commits", url=data.get("html_url") or fallback_url,
                body_md="Commit messages between the two releases (low confidence, no release notes found):\n"
                + "\n".join(lines),
            )
    return None


# --- entry point ----------------------------------------------------------------------------------------------


async def fetch_notes(
    gh: GitHubClient,
    conn: sqlite3.Connection,
    source: SourceRepo,
    info: PackageInfo,
    current: str,
    target: str,
) -> NotesResult:
    """Notes for every version in (current, target]. Cached notes are reused and never refetched."""
    versions = versions_in_range(info, current, target)
    result = NotesResult(versions=versions, notes={})
    if not versions:
        return result

    name = info.name
    notes = store.load_release_notes(conn, info.ecosystem, name, versions)
    missing = set(versions) - set(notes)

    if missing and source.status == "resolved":
        fetched: list[ReleaseNote] = []
        releases = await _from_github_releases(gh, source, name, missing, current)
        fetched += releases.values()
        still = missing - set(releases)
        if still:
            files = await _from_changelog_files(gh, source, still, conn, info.ecosystem, name)
            fetched += files.values()
        if not notes and not fetched:
            fallback = await _from_compare(gh, source, name, current, target)
            if fallback:
                fetched.append(fallback)
        if fetched:
            store.save_release_notes(conn, info.ecosystem, name, fetched)
            notes = store.load_release_notes(conn, info.ecosystem, name, versions)

    result.notes = {v: notes[v] for v in versions if v in notes}
    result.coverage = round(len(result.notes) / len(versions), 3)
    return result
