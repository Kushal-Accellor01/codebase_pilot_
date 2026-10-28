import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.updates.registry.base import PackageInfo, VersionInfo
from app.updates.registry.npm import parse_package
from app.updates.versions import (
    ConstraintError,
    normalize_constraint,
    parse_scan_time,
    resolve_versions,
    update_type,
)

SCAN = datetime(2026, 10, 26, 15, 50, 15, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent.parent / "fixtures" / "npm"


def pkg(versions: list[str], latest: str | None = None, **kw) -> PackageInfo:
    """Package whose versions were all published well before the scan date."""
    return PackageInfo(
        ecosystem="npm",
        name="p",
        latest_version=latest or versions[-1],
        versions=[
            VersionInfo(version=v, published_at="2020-01-01T00:00:00.000Z", is_prerelease="-" in v)
            for v in versions
        ],
        **kw,
    )


# --- the six required cases from the spec (5.3) ---------------------------------------

@pytest.mark.parametrize(
    "constraint,versions,expected,basis",
    [
        ("^1.18.1", ["1.18.0", "1.18.1", "1.18.2", "2.0.0"], "1.18.2", "max_satisfying_at_scan"),
        ("~3.0.2", ["3.0.2", "3.0.5", "3.1.0"], "3.0.5", "max_satisfying_at_scan"),
        ("1.2.1", ["1.2.1", "1.3.0"], "1.2.1", "exact_pin"),
        ("^0.7.1", ["0.7.1", "0.7.2", "0.8.0", "1.0.0"], "0.7.2", "max_satisfying_at_scan"),
        ("^15.0.3", ["15.0.3", "16.0.0-beta.1"], "15.0.3", "max_satisfying_at_scan"),
    ],
)
def test_required_current_version_cases(constraint, versions, expected, basis):
    v = resolve_versions(constraint, pkg(versions, latest=versions[-1]), SCAN)
    assert (v.current_version, v.current_version_basis) == (expected, basis)
    assert v.error is None


def test_unsatisfiable_range_is_unresolvable():
    v = resolve_versions("^2.0.0", pkg(["1.9.0"]), SCAN)
    assert v.current_version is None
    assert v.current_version_basis == "unresolvable"
    assert "no published version" in v.error


# --- scan-date logic --------------------------------------------------------------------

def test_versions_published_after_scan_are_not_current():
    info = pkg(["1.0.0", "1.1.0"])
    info.versions[1].published_at = "2026-11-01T00:00:00.000Z"
    v = resolve_versions("^1.0.0", info, SCAN)
    assert v.current_version == "1.0.0"
    assert v.latest_in_range == "1.1.0"  # latest-in-range uses everything published so far
    assert v.update_type == "minor" and v.versions_behind == 1
    assert not v.requires_manifest_change


def test_published_at_exactly_scan_time_counts():
    info = pkg(["1.0.0", "1.1.0"])
    info.versions[1].published_at = "2026-10-26T15:50:15.000Z"
    assert resolve_versions("^1.0.0", info, SCAN).current_version == "1.1.0"


def test_missing_publish_time_is_included():
    info = pkg(["1.0.0", "1.1.0"])
    info.versions[1].published_at = None
    assert resolve_versions("^1.0.0", info, SCAN).current_version == "1.1.0"


def test_lockfile_wins():
    v = resolve_versions("^1.0.0", pkg(["1.0.0", "1.5.0"]), SCAN, lockfile_version="1.0.0")
    assert (v.current_version, v.current_version_basis) == ("1.0.0", "lockfile")


def test_parse_scan_time():
    assert parse_scan_time("2026-10-26 15:50:15") == SCAN


# --- update type, behind, manifest change ------------------------------------------------

def test_major_update_requires_manifest_change():
    v = resolve_versions("^4.21.2", pkg(["4.21.2", "4.22.0", "5.0.0", "5.1.0", "5.2.0-beta.1"], latest="5.1.0"), SCAN)
    assert v.current_version == "4.22.0"
    assert v.latest_version == "5.1.0"
    assert v.latest_in_range == "4.22.0"
    assert v.update_type == "major"
    assert v.requires_manifest_change
    assert v.versions_behind == 2  # 5.0.0 and 5.1.0; the beta is not counted


def test_up_to_date():
    v = resolve_versions("^1.0.0", pkg(["1.0.0", "1.2.0"]), SCAN)
    assert v.update_type == "none" and v.versions_behind == 0
    assert not v.requires_manifest_change and not v.runtime_requirement_change


def test_exact_pin_behind_needs_manifest_change():
    v = resolve_versions("1.2.1", pkg(["1.2.1", "1.2.2", "1.3.0"]), SCAN)
    assert v.latest_in_range == "1.2.1"
    assert v.update_type == "minor" and v.requires_manifest_change and v.versions_behind == 2


def test_pin_not_in_registry_still_current():
    v = resolve_versions("9.9.9", pkg(["1.0.0"]), SCAN)
    assert v.current_version == "9.9.9" and v.update_type == "none"


def test_latest_tag_lower_than_current_is_none():
    v = resolve_versions("^2.0.0", pkg(["1.9.0", "2.0.0", "2.1.0"], latest="1.9.0"), SCAN)
    assert v.current_version == "2.1.0" and v.update_type == "none"
    assert not v.requires_manifest_change


@pytest.mark.parametrize(
    "a,b,expected",
    [("1.2.3", "2.0.0", "major"), ("1.2.3", "1.3.0", "minor"), ("1.2.3", "1.2.4", "patch"),
     ("1.2.3", "1.2.3", "none"), ("1.2.3", "1.0.0", "none"), (None, "1.0.0", "unknown")],
)
def test_update_type(a, b, expected):
    assert update_type(a, b) == expected


# --- pre-releases -------------------------------------------------------------------------

def test_prerelease_ignored_for_latest_in_range_and_behind():
    v = resolve_versions("^1.0.0", pkg(["1.0.0", "1.1.0-rc.1"], latest="1.0.0"), SCAN)
    assert v.latest_in_range == "1.0.0" and v.versions_behind == 0


def test_prerelease_constraint_allows_prereleases():
    v = resolve_versions("^2.0.0-beta.1", pkg(["2.0.0-beta.1", "2.0.0-beta.2"], latest="2.0.0-beta.2"), SCAN)
    assert v.current_version == "2.0.0-beta.2"


# --- engines and deprecation ---------------------------------------------------------------

def test_runtime_requirement_change():
    info = pkg(["4.0.0", "5.0.0"])
    info.versions[0].engines = {"node": ">= 0.10"}
    info.versions[1].engines = {"node": ">= 18"}
    v = resolve_versions("^4.0.0", info, SCAN)
    assert v.runtime_requirement_change
    assert (v.engines_current, v.engines_target) == (">= 0.10", ">= 18")


def test_same_engines_no_change():
    info = pkg(["4.0.0", "4.1.0"])
    for x in info.versions:
        x.engines = {"node": ">= 18"}
    assert not resolve_versions("^3.0.0 || ^4.0.0", info, SCAN).runtime_requirement_change


def test_deprecated_latest():
    info = pkg(["1.0.0", "1.1.0"], is_deprecated=True, deprecation_message="use X")
    v = resolve_versions("^1.0.0", info, SCAN)
    assert v.is_deprecated and v.deprecation_message == "use X"


def test_deprecated_current_only():
    info = pkg(["1.0.0", "1.0.1"])
    info.versions[0].deprecation_message = "bad release"
    v = resolve_versions("1.0.0", info, SCAN)
    assert v.is_deprecated and v.deprecation_message == "bad release"


def test_real_fixture_left_pad_is_deprecated():
    info = parse_package("left-pad", json.loads((FIXTURES / "left-pad.json").read_text(encoding="utf-8")))
    v = resolve_versions("^1.1.3", info, parse_scan_time("2026-10-26 15:50:15"))
    assert v.is_deprecated and v.current_version == "1.3.0" and v.update_type == "none"


def test_real_fixture_morgan_pin():
    doc = json.loads((FIXTURES / "morgan.json").read_text(encoding="utf-8"))
    v = resolve_versions("1.10.0", parse_package("morgan", doc), parse_scan_time("2026-10-26 15:50:15"))
    assert v.current_version == "1.10.0" and v.latest_version == "1.12.1"
    assert v.update_type == "minor" and v.requires_manifest_change and v.versions_behind >= 2


def test_real_fixture_morgan_as_of_old_scan():
    """Time travel: on 2020-01-01 the newest ^1.9.0 morgan was 1.10.0 or older, never 1.12.x."""
    doc = json.loads((FIXTURES / "morgan.json").read_text(encoding="utf-8"))
    v = resolve_versions("^1.9.0", parse_package("morgan", doc), parse_scan_time("2020-01-01 00:00:00"))
    assert v.current_version in ("1.9.0", "1.9.1", "1.10.0")
    assert v.latest_in_range == "1.12.1"


# --- bad input -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad", ["git+https://github.com/a/b.git", "file:../x", "workspace:*", "npm:foo@1", "owner/repo",
            "bad range", "next"],
)
def test_non_registry_constraints_error(bad):
    with pytest.raises(ConstraintError):
        normalize_constraint(bad)
    v = resolve_versions(bad, pkg(["1.0.0"]), SCAN)
    assert v.error and v.current_version is None


@pytest.mark.parametrize("c", [None, "", "*", "latest", "x"])
def test_any_version(c):
    assert normalize_constraint(c) == "*"
    assert resolve_versions(c, pkg(["1.0.0", "2.0.0"]), SCAN).current_version == "2.0.0"


@pytest.mark.parametrize("c", ["^1.0.0 || ^2.0.0", ">=1.0.0 <2", "1.x", "~1", "1.0.0 - 1.5.0"])
def test_complex_ranges_ok(c):
    assert normalize_constraint(c) == c
    assert resolve_versions(c, pkg(["1.0.0", "1.4.0", "2.0.0"]), SCAN).current_version


def test_non_semver_versions_skipped_and_counted():
    info = pkg(["1.0.0", "1.1.0"])
    info.versions.append(VersionInfo(version="not-a-version"))
    info.versions.append(VersionInfo(version="1.0"))
    v = resolve_versions("^1.0.0", info, SCAN)
    assert v.skipped_versions == 2 and v.current_version == "1.1.0"


def test_empty_registry_data():
    v = resolve_versions("^1.0.0", PackageInfo(ecosystem="npm", name="p"), SCAN)
    assert v.current_version is None and v.error
