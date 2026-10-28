"""Step 5: the remediation plan. Numbers come from planning.risk; the LLM writes the wording only."""
import logging

from pydantic import BaseModel, Field

from app.core.contracts import DependencyChangeEvent, Finding, Plan, ProbeResult, TelemetryEvidence
from app.core.llm import LLM, LLMError
from app.planning.risk import active_findings, effort_hours, severity
from app.updates.analysis import load_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "plan_v1"


class PlanText(BaseModel):
    rationale: str = Field(max_length=900)
    steps: list[str] = Field(min_length=1, max_length=8)
    validation: list[str] = Field(min_length=1, max_length=6)


def _probe_text(probe: ProbeResult | None) -> str:
    if probe is None:
        return "not run"
    if probe.timed_out:
        return "timed out"
    counts = f" ({probe.tests_passed} of {probe.tests_total} tests pass)" if probe.tests_total else ""
    return ("passed" if probe.passed else "failed") + counts


def _facts(event: DependencyChangeEvent, findings: list[Finding], probe, telemetry, sev: str, hours: float) -> dict:
    changes = "\n".join(
        f"- [{'breaking' if c.breaking else 'note'}] {c.description} ({', '.join(c.api_symbols) or 'no symbol'}), "
        f"in {c.version}" for c in event.changes
    ) or "none extracted"
    lines = "\n".join(
        f"- {f.file_path}:{f.line} {f.symbol or ''} (found by {', '.join(f.sources)}): {f.snippet}"
        for f in active_findings(findings)
    ) or "none"
    prod = "; ".join(f"{t.count} log lines: {t.signature[:120]}" for t in telemetry) or "none"
    return {
        "package": event.package, "current_version": event.current_version, "target_version": event.target_version,
        "update_type": event.update_type, "severity": sev, "effort_hours": hours,
        "summary": event.summary or "not available", "changes": changes, "findings": lines,
        "upgrade_probe": _probe_text(probe), "telemetry": prod,
    }


def fallback_text(event: DependencyChangeEvent, findings: list[Finding], sev: str, probe) -> PlanText:
    """Deterministic wording used when the model is unavailable, so a run never stalls on the LLM."""
    live = active_findings(findings)
    files = sorted({f.file_path for f in live})
    rationale = (
        f"Upgrading {event.package} {event.current_version} to {event.target_version} is rated {sev}: "
        f"{len(live)} affected line(s) in {len(files)} file(s); tests on the new version {_probe_text(probe)}."
    )
    steps = [f"Upgrade {event.package} to {event.target_version}."]
    steps += [f"Update {f.file_path}:{f.line} ({f.symbol or 'affected call'})." for f in live[:6]]
    steps.append("Run the full test suite.")
    return PlanText(
        rationale=rationale, steps=steps[:8],
        validation=["All tests pass on the new version.", "No deprecation warnings for the changed APIs remain."],
    )


async def build_plan(
    llm: LLM | None,
    event: DependencyChangeEvent,
    findings: list[Finding],
    upgrade_probe: ProbeResult | None,
    telemetry: list[TelemetryEvidence],
    *,
    manifest_changes: bool = True,
) -> Plan:
    sev = severity(event, findings, upgrade_probe, telemetry)
    hours = effort_hours(findings, manifest_changes)
    text = None
    if llm is not None:
        system, template = load_prompt(PROMPT_VERSION)
        try:
            text = await llm.chat(
                PlanText, prompt_version=PROMPT_VERSION,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": template.format(**_facts(event, findings, upgrade_probe,
                                                                               telemetry, sev, hours))}],
            )
        except LLMError as exc:
            logger.warning("plan wording fell back to the template: %s", exc)
    text = text or fallback_text(event, findings, sev, upgrade_probe)
    return Plan(severity=sev, effort_hours=hours, rationale=text.rationale, steps=text.steps,
                validation=text.validation)


def route_after_plan(plan: Plan) -> str:
    """low -> report only; anything else goes on to a fix."""
    return "report_only" if plan.severity == "low" else "full"
