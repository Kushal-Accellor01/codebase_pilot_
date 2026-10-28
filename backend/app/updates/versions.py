"""Resolve current / latest-in-range / latest versions for an npm dependency (npm range semantics)."""
import re
from datetime import datetime, timezone
from functools import cmp_to_key

import nodesemver as semver

from app.updates.models import UpdateVerdict
from app.updates.registry.base import PackageInfo, VersionInfo

_NON_REGISTRY_PREFIXES = (
    "git+", "git:", "git@", "github:", "gitlab:", "bitbucket:", "http:", "https:", "file:", "link:",
    "workspace:", "npm:", "portal:", "patch:", "./", "../", "/", "~/",
)
_PRERELEASE_IN_RANGE = re.compile(r"\d+\.\d+\.\d+-[0-9A-Za-z]")


class ConstraintError(ValueError):
    """The declared constraint is not a semver range from the registry (URL, tag, alias, junk)."""


def normalize_constraint(constraint: str | None) -> str:
    """Return a range string node-semver understands, or raise ConstraintError."""
    text = (constraint or "").strip()
    if text.lower() in ("", "*", "latest", "x"):
        return "*"
    if text.startswith(_NON_REGISTRY_PREFIXES) or re.fullmatch(r"[\w.-]+/[\w.-]+(#.*)?", text):
        raise ConstraintError(f"not a registry version range: {constraint!r}")
    if semver.valid_range(text, False) is None:
        raise ConstraintError(f"invalid version range: {constraint!r}")
    return text


def clean_version(text: str) -> str | None:
    """'v1.2.3' -> '1.2.3'; None if not a full semver version (node-semver returns a SemVer object)."""
    parsed = semver.valid(text.strip(), False)
    return parsed.version if parsed else None


def exact_pin(range_: str) -> str | None:
    """'1.2.3', '=1.2.3', 'v1.2.3' -> '1.2.3'; anything that is a real range -> None."""
    return clean_version(range_.lstrip("= ")) if range_ != "*" else None


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_scan_time(scanned_at: str) -> datetime:
    """scans.scanned_at is UTC text 'YYYY-MM-DD HH:MM:SS' (SQLite CURRENT_TIMESTAMP)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(scanned_at, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognised scanned_at: {scanned_at!r}")


def _cmp(a: str, b: str) -> int:
    return semver.compare(a, b, False)


def _max(versions: list[str], range_: str, include_prerelease: bool) -> str | None:
    return semver.max_satisfying(versions, range_, False, include_prerelease)


def update_type(current: str | None, latest: str | None) -> str:
    if not current or not latest:
        return "unknown"
    if _cmp(latest, current) <= 0:
        return "none"
    old, new = semver.SemVer(current, False, False), semver.SemVer(latest, False, False)
    if new.major != old.major:
        return "major"
    if new.minor != old.minor:
        return "minor"
    return "patch"


def resolve_versions(
    constraint: str | None,
    info: PackageInfo,
    scan_time: datetime,
    *,
    lockfile_version: str | None = None,
) -> UpdateVerdict:
    """Deterministic version facts for one dependency.

    current_version, first match wins:
      lockfile -> exact pin -> highest version satisfying the range published on or before the scan
      (what `npm install` would have installed on scan day). Pre-releases are ignored unless the
      declared range is itself a pre-release.
    """
    verdict = UpdateVerdict(latest_version=info.latest_version)
    try:
        range_ = normalize_constraint(constraint)
    except ConstraintError as exc:
        verdict.error = str(exc)
        verdict.current_version_basis = "unresolvable"
        return verdict

    include_pre = bool(_PRERELEASE_IN_RANGE.search(range_))
    by_version: dict[str, VersionInfo] = {}
    for v in info.versions:
        clean = clean_version(v.version)
        if clean is None:
            verdict.skipped_versions += 1
            continue
        if v.is_prerelease and not include_pre:
            continue
        by_version[clean] = v
    all_versions = sorted(by_version, key=cmp_to_key(_cmp))

    if verdict.latest_version is None and all_versions:
        verdict.latest_version = all_versions[-1]
    if verdict.latest_version:
        verdict.latest_version = clean_version(verdict.latest_version) or verdict.latest_version

    pin = exact_pin(range_)
    published_by_scan = [
        v for v in all_versions
        if (t := _parse_time(by_version[v].published_at)) is None or t <= scan_time
    ]

    if lockfile_version:
        verdict.current_version, verdict.current_version_basis = lockfile_version, "lockfile"
    elif pin:
        verdict.current_version, verdict.current_version_basis = pin, "exact_pin"
    else:
        verdict.current_version = _max(published_by_scan, range_, include_pre)
        verdict.current_version_basis = (
            "max_satisfying_at_scan" if verdict.current_version else "unresolvable"
        )
    if verdict.current_version is None:
        verdict.error = f"no published version satisfies {constraint!r} on the scan date"
        return verdict

    current, latest = verdict.current_version, verdict.latest_version
    verdict.latest_in_range = pin or _max(all_versions, range_, include_pre)
    verdict.update_type = update_type(current, latest)
    verdict.requires_manifest_change = bool(
        latest and _cmp(latest, current) > 0 and not semver.satisfies(latest, range_, False, include_pre)
    )
    if latest:
        verdict.versions_behind = sum(1 for v in all_versions if _cmp(v, current) > 0 and _cmp(v, latest) <= 0)

    cur_info, latest_info = by_version.get(current), by_version.get(latest or "")
    if cur_info and latest_info and verdict.update_type != "none":
        cur_node = cur_info.engines.get("node")
        latest_node = latest_info.engines.get("node")
        verdict.runtime_requirement_change = cur_node != latest_node
        verdict.engines_current, verdict.engines_target = cur_node, latest_node

    # Deprecated if the package's latest release is, or the version we are on is.
    if info.is_deprecated:
        verdict.is_deprecated, verdict.deprecation_message = True, info.deprecation_message
    elif cur_info and cur_info.deprecation_message:
        verdict.is_deprecated, verdict.deprecation_message = True, cur_info.deprecation_message
    return verdict
