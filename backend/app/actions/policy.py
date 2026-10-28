"""C2: what Pilot is allowed to do after a human decision. Code decides; nothing here calls the model."""
from typing import Literal

from app.core.contracts import Finding, PatchResult, Plan
from app.orchestrator.config_loader import RepoPilotConfig

ActionDecision = Literal["none", "report_only", "issue", "pull_request"]


def decide_action(
    *,
    decision: str | None,
    config: RepoPilotConfig,
    plan: Plan,
    findings: list[Finding],
    latest_patch: PatchResult | None,
) -> ActionDecision:
    """
    none          not approved, Pilot disabled, or shadow mode
    issue         advisory mode; or the fix is not green; or a finding is below the confidence floor
    pull_request  approved, tests green, every active finding at or above min_confidence
    """
    if decision != "approved" or not config.enabled or config.mode == "shadow":
        return "none"
    if config.mode == "advisory":
        return "issue"
    tests_green = bool(latest_patch and latest_patch.validation and latest_patch.validation.passed)
    active = [f for f in findings if f.confirmed is not False]
    if not tests_green or (active and min(f.confidence for f in active) < config.min_confidence):
        return "issue"
    return "pull_request"
