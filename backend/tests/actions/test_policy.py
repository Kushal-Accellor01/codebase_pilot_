import pytest

from app.actions.policy import decide_action
from app.core.contracts import Finding, PatchResult, Plan, ProbeResult
from app.orchestrator.config_loader import RepoPilotConfig

PLAN = Plan(severity="high", effort_hours=2, rationale="r", steps=["s"], validation=["v"])


def patch(passed: bool | None) -> PatchResult | None:
    if passed is None:
        return None
    probe = ProbeResult(command="npm test", exit_code=0 if passed else 1, passed=passed, duration_ms=1, output_tail="")
    return PatchResult(attempt=1, method="llm_edits", edits=[], diff="", manifest_change=None, validation=probe)


def finding(confidence=0.9, confirmed=None) -> Finding:
    return Finding(file_path="a.js", line=1, snippet="x", sources=["pattern"], confidence=confidence, confirmed=confirmed)


def decide(decision="approved", config=None, findings=None, latest=True):
    return decide_action(
        decision=decision, config=config or RepoPilotConfig(), plan=PLAN,
        findings=[finding()] if findings is None else findings, latest_patch=patch(latest),
    )


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(), "pull_request"),
        (dict(decision="rejected"), "none"),
        (dict(decision=None), "none"),
        (dict(config=RepoPilotConfig(enabled=False)), "none"),
        (dict(config=RepoPilotConfig(mode="shadow")), "none"),
        (dict(config=RepoPilotConfig(mode="advisory")), "issue"),
        (dict(config=RepoPilotConfig(mode="enforce")), "pull_request"),
        (dict(latest=False), "issue"),  # tests are red: hand over to an engineer
        (dict(latest=None), "issue"),  # no patch at all
        (dict(findings=[finding(0.4)]), "issue"),  # below the confidence floor
        (dict(findings=[finding(0.4, confirmed=False)]), "pull_request"),  # rejected findings don't count
        (dict(findings=[]), "pull_request"),
        (dict(findings=[finding(0.9), finding(0.49)]), "issue"),
        (dict(findings=[finding(0.5)]), "pull_request"),  # exactly at the floor is fine
        (dict(config=RepoPilotConfig(min_confidence=0.95)), "issue"),
    ],
)
def test_policy_table(kwargs, expected):
    assert decide(**kwargs) == expected


def test_shadow_and_disabled_win_even_over_advisory_and_red_tests():
    assert decide(config=RepoPilotConfig(mode="shadow"), latest=False) == "none"
    assert decide(decision="rejected", config=RepoPilotConfig(mode="advisory")) == "none"
