"""Plan severity and effort, all computed in code (BS 7.3). The LLM only words the rationale."""
import math

from app.core.contracts import DependencyChangeEvent, Finding, ProbeResult, TelemetryEvidence

SEVERITY_ORDER = ("low", "medium", "high", "critical")


def active_findings(findings: list[Finding]) -> list[Finding]:
    """Findings still standing: the LLM may reject a pattern-only match, nothing else removes one."""
    return [f for f in findings if f.confirmed is not False]


def _is_breaking(finding: Finding, event: DependencyChangeEvent) -> bool:
    if "test_failure" in finding.sources:
        return True  # the upgrade actually broke a test here
    change = next((c for c in event.changes if c.id is not None and c.id == finding.change_id), None)
    return bool(change and change.breaking)


def _only_deprecations(findings: list[Finding], event: DependencyChangeEvent) -> bool:
    if not findings:
        return False
    kinds = {c.id: c.kind for c in event.changes}
    return all(
        "test_failure" not in f.sources
        and (kinds.get(f.change_id) == "deprecated" or f.sources == ["runtime_deprecation"])
        for f in findings
    )


def severity(
    event: DependencyChangeEvent,
    findings: list[Finding],
    upgrade_probe: ProbeResult | None,
    telemetry: list[TelemetryEvidence],
) -> str:
    """First matching rule wins.

    critical  the upgrade fails the tests and production logs show the affected path; or a critical/high vulnerability
    high      the upgrade fails the tests; or a confirmed finding with a breaking change
    medium    only deprecation findings; or a new runtime requirement; or a medium vulnerability
    low       no findings and the upgrade passes the tests
    """
    live = active_findings(findings)
    probe_failed = bool(upgrade_probe and not upgrade_probe.passed)
    vuln = {v.severity for v in event.vulnerabilities}

    if (probe_failed and telemetry) or vuln & {"critical", "high"}:
        return "critical"
    if probe_failed or any(_is_breaking(f, event) for f in live):
        return "high"
    if _only_deprecations(live, event) or event.runtime_requirement_change or "medium" in vuln:
        return "medium"
    if live:
        return "medium"  # findings exist but none is clearly breaking: still needs a look
    return "low"


def effort_hours(findings: list[Finding], manifest_changes: bool) -> float:
    """0.25 h per confirmed finding + 0.5 h per distinct file + 1 h if the manifest changes + 1 h review and
    deploy, rounded up to the nearest half hour."""
    live = active_findings(findings)
    raw = 0.25 * len(live) + 0.5 * len({f.file_path for f in live}) + (1.0 if manifest_changes else 0.0) + 1.0
    return math.ceil(raw * 2 - 1e-9) / 2
