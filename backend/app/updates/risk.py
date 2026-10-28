"""Priority score and risk level. Rules in code: predictable, explainable, testable (S2 5.5 and 5.10)."""
import math
from dataclasses import dataclass, field

from app.updates.models import UpdateVerdict
from app.updates.versions import compare_versions as _cmp

SEVERITY_POINTS = {"critical": 100, "high": 80, "medium": 50, "low": 30, "unknown": 40}
UPDATE_POINTS = {"major": 30, "minor": 10, "patch": 3, "none": 0, "unknown": 0}
TYPE_WEIGHT = {"runtime": 1.0, "peer": 1.0, "optional": 0.7, "dev": 0.5, "unknown": 0.8}

# Dependencies that ship with (or can break) the running application.
RUNTIME_LIKE = {"runtime", "peer", "unknown"}


@dataclass
class RiskFacts:
    update_type: str = "none"
    dependency_type: str = "unknown"
    importer_count: int = 0
    is_deprecated: bool = False
    latest_deprecated: bool = False
    runtime_requirement_change: bool = False
    vuln_severities: list[str] = field(default_factory=list)
    has_breaking: bool = False  # from validated release-note analysis; False until notes are analysed
    archived: bool = False
    notes_unavailable: bool = False

    @classmethod
    def from_verdict(cls, v: UpdateVerdict, dependency_type: str, importer_count: int) -> "RiskFacts":
        return cls(
            update_type=v.update_type,
            dependency_type=dependency_type,
            importer_count=importer_count,
            is_deprecated=v.is_deprecated,
            latest_deprecated=v.latest_deprecated,
            runtime_requirement_change=v.runtime_requirement_change,
        )


def needs_attention(f: RiskFacts) -> bool:
    """Only these continue to source/notes/analysis; the rest are 'up_to_date'."""
    return f.update_type not in ("none", "unknown") or bool(f.vuln_severities) or f.is_deprecated


def priority(f: RiskFacts) -> float:
    if not needs_attention(f):
        return 0.0  # importer count alone must not rank an up-to-date package
    score = max((SEVERITY_POINTS.get(s, SEVERITY_POINTS["unknown"]) for s in f.vuln_severities), default=0)
    score += 40 if f.is_deprecated else 0
    score += UPDATE_POINTS.get(f.update_type, 0)
    score += 15 if f.runtime_requirement_change else 0
    score += min(20, 5 * math.log2(1 + f.importer_count))
    return round(score * TYPE_WEIGHT.get(f.dependency_type, TYPE_WEIGHT["unknown"]), 1)


def risk_level(f: RiskFacts) -> str:
    """First match wins. 'has_breaking' is False until release notes have been analysed, so a run
    without notes gives a provisional level driven by vulnerabilities, deprecation and engines."""
    sev = set(f.vuln_severities)
    runtime_like = f.dependency_type in RUNTIME_LIKE
    deprecated_no_replacement = f.is_deprecated and f.latest_deprecated

    if "critical" in sev or ("high" in sev and runtime_like):
        return "critical"
    if (
        "high" in sev
        or (f.has_breaking and f.importer_count > 0 and runtime_like)
        or deprecated_no_replacement
    ):
        return "high"
    if f.has_breaking or f.runtime_requirement_change or "medium" in sev or f.archived:
        return "medium"
    if needs_attention(f) or f.notes_unavailable:
        return "low"
    return "none"


def recommended_target(
    v: UpdateVerdict, min_safe_version: str | None = None, min_safe_in_range: bool = False
) -> str | None:
    """Vulnerabilities: the lowest version that clears them (if in range), else latest.
    Otherwise latest-in-range when it is ahead of us, else latest. Never a downgrade."""
    current = v.current_version
    if min_safe_version and (current is None or _cmp(min_safe_version, current) > 0):
        return min_safe_version if min_safe_in_range else v.latest_version
    for candidate in (v.latest_in_range, v.latest_version):
        if candidate and (current is None or _cmp(candidate, current) > 0):
            return candidate
    return current
