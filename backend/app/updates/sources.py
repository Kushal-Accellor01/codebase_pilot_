"""Find and verify the source repository of a package (S2 5.6).

Chain, first verified source wins:
  override file -> registry `repository` -> deps.dev -> registry `homepage` -> source_unknown.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

from app.core.config import get_settings
from app.updates import store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from app.updates.models import SourceRepo
from app.updates.registry.base import PackageInfo

logger = logging.getLogger(__name__)

DEPS_DEV = "https://api.deps.dev/v3"
RESOLVED_TTL_DAYS = 30
UNKNOWN_TTL_DAYS = 1  # look again soon: a person may add an override

DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "config" / "package_sources.yaml"


def overrides_path() -> Path:
    return Path(get_settings().package_sources_path or DEFAULT_OVERRIDES_PATH)


# --- parsing ------------------------------------------------------------------------------------


def parse_github_repo(repository) -> tuple[str, str, str | None] | None:
    """npm 'repository' is a string or {type, url, directory}. Returns (owner, repo, directory)."""
    directory = None
    if isinstance(repository, dict):
        directory = repository.get("directory")
        repository = repository.get("url")
    if not repository or not isinstance(repository, str):
        return None
    s = repository.strip()
    m = re.fullmatch(r"(?:github:)?([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", s)
    if not m:
        m = re.search(r"github\.com[/:]([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/#?].*)?$", s)
    if not m:
        return None
    return m.group(1), m.group(2), directory


def load_overrides(path: Path | None = None) -> dict[str, dict]:
    """{'npm/express': {owner, repo, directory?, changelog_path?}}. Missing or empty file -> {}."""
    path = path or overrides_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of 'ecosystem/name' to source settings")
    return data


def save_override(path: Path, ecosystem: str, name: str, owner: str, repo: str, directory: str | None = None) -> None:
    """Add or replace one override (used by the manual-mapping API)."""
    data = load_overrides(path)
    entry = {"owner": owner, "repo": repo}
    if directory:
        entry["directory"] = directory
    data[f"{ecosystem}/{name}"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


# --- verification ---------------------------------------------------------------------------------


async def verify_repo(
    gh: GitHubClient, package: str, owner: str, repo: str, directory: str | None
) -> SourceRepo | None:
    """The repo's package.json (in `directory` for monorepos) must carry this package's name."""
    prefix = f"{directory.strip('/')}/" if directory else ""
    text = await gh.get_text(f"repos/{owner}/{repo}/contents/{prefix}package.json")
    if text is None:
        return None
    try:
        name = json.loads(text).get("name")
    except ValueError:
        return None
    if name != package:
        logger.info("source %s/%s rejected: package.json name %r != %r", owner, repo, name, package)
        return None
    meta = await gh.get_json(f"repos/{owner}/{repo}") or {}
    return SourceRepo(
        owner=meta.get("owner", {}).get("login", owner),
        repo=meta.get("name", repo),
        directory=directory or None,
        default_branch=meta.get("default_branch"),
        archived=bool(meta.get("archived")),
        verified=True,
    )


async def deps_dev_repo(http: HttpClient, package: str, version: str) -> tuple[str, str] | None:
    url = f"{DEPS_DEV}/systems/npm/packages/{quote(package, safe='')}/versions/{quote(version, safe='')}"
    resp = await http.get("deps_dev", url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    for project in resp.json().get("relatedProjects", []):
        if project.get("relationType") != "SOURCE_REPO":
            continue
        key = project.get("projectKey", {}).get("id", "")
        if key.startswith("github.com/"):
            parts = key.split("/")
            if len(parts) >= 3:
                return parts[1], parts[2]
    return None


def homepage_repo(homepage: str | None) -> tuple[str, str] | None:
    if not homepage:
        return None
    m = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/#?].*)?$", homepage)
    return (m.group(1), m.group(2)) if m else None


# --- the chain ----------------------------------------------------------------------------------------


async def resolve_source(
    gh: GitHubClient,
    http: HttpClient,
    info: PackageInfo,
    version: str,
    overrides: dict[str, dict] | None = None,
) -> SourceRepo:
    """Walk the chain and return the first source that verifies, or an 'unknown' SourceRepo."""
    overrides = overrides if overrides is not None else load_overrides()
    name = info.name
    tried: set[tuple[str, str, str | None]] = set()

    async def attempt(method: str, confidence: str, owner: str, repo: str, directory: str | None, **extra):
        key = (owner.lower(), repo.lower(), directory)
        if key in tried:
            return None
        tried.add(key)
        found = await verify_repo(gh, name, owner, repo, directory)
        if found:
            found.method, found.confidence = method, confidence
            found.changelog_path = extra.get("changelog_path")
        return found

    override = overrides.get(f"npm/{name}")
    if override and override.get("owner") and override.get("repo"):
        found = await attempt(
            "override", "high", override["owner"], override["repo"], override.get("directory"),
            changelog_path=override.get("changelog_path"),
        )
        if found:
            return found
        logger.warning("override for %s did not verify; trying the normal chain", name)

    parsed = parse_github_repo(info.repository)
    if parsed:
        found = await attempt("registry", "high", parsed[0], parsed[1], parsed[2])
        if found:
            return found

    deps = await deps_dev_repo(http, name, version)
    if deps:
        # deps.dev doesn't know the monorepo directory: reuse the one the registry gave, if any.
        found = await attempt("deps_dev", "high", deps[0], deps[1], parsed[2] if parsed else None)
        if found:
            return found

    home = homepage_repo(info.homepage)
    if home:
        found = await attempt("homepage", "medium", home[0], home[1], parsed[2] if parsed else None)
        if found:
            return found

    return SourceRepo(owner="", repo="", method="manual", confidence="low", status="unknown")


async def get_source(
    gh: GitHubClient,
    http: HttpClient,
    conn: sqlite3.Connection,
    info: PackageInfo,
    version: str,
    *,
    overrides: dict[str, dict] | None = None,
    refresh: bool = False,
) -> SourceRepo:
    """resolve_source with the 30-day cache in `package_sources`. Network calls happen before the write."""
    cached = store.load_package_source(conn, info.ecosystem, info.name)
    if cached and not refresh:
        source, resolved_at = cached
        ttl = RESOLVED_TTL_DAYS if source.status == "resolved" else UNKNOWN_TTL_DAYS
        has_override = f"{info.ecosystem}/{info.name}" in (overrides or {})
        override_changed = has_override and source.method != "override"  # a new override beats the cache
        if datetime.now(timezone.utc) - resolved_at < timedelta(days=ttl) and not override_changed:
            return source
    source = await resolve_source(gh, http, info, version, overrides)
    if cached and cached[0].changelog_path and not source.changelog_path:
        source.changelog_path = cached[0].changelog_path  # keep what notes.py learned
    store.save_package_source(conn, info.ecosystem, info.name, source)
    return source
