import asyncio

import pytest

from app.core import db as core_db
from app.core.config import Settings
from app.core.contracts import DependencyChangeEvent, Finding, Plan, ProbeResult, TelemetryEvidence
from app.core.llm import LLM, LLMOutputError
from app.planning.plan import PlanText, build_plan, fallback_text, route_after_plan
from app.planning.risk import effort_hours, severity
from app.updates.models import ChangeRef, VulnRef
from tests.updates.conftest import make_db


def event(**kw) -> DependencyChangeEvent:
    base = dict(
        event_id="s:npm:express:4.21.2->5.3.0", scan_id="s", ecosystem="npm", package="express",
        dependency_type="runtime", manifest_path="package.json", current_version="4.21.2",
        current_version_basis="exact_pin", target_version="5.3.0", update_type="major",
        requires_manifest_change=True, runtime_requirement_change=False, is_deprecated=False, importer_count=4,
        priority_score=60.0, risk_level="high", status="analyzed",
        changes=[ChangeRef(id=1, kind="removed", breaking=True, api_symbols=["req.param"], description="req.param removed",
                           version="5.0.0", evidence_url="u", evidence_quote="q"),
                 ChangeRef(id=2, kind="deprecated", breaking=False, api_symbols=["res.old"], description="res.old deprecated",
                           version="5.0.0", evidence_url="u", evidence_quote="q")],
    )
    return DependencyChangeEvent(**{**base, **kw})


def finding(file="a.js", line=1, sources=("pattern",), change_id=None, confirmed=None, symbol="req.param") -> Finding:
    return Finding(file_path=file, line=line, snippet="x", symbol=symbol, change_id=change_id, sources=list(sources),
                   confidence=0.8, confirmed=confirmed)


def probe(passed: bool) -> ProbeResult:
    return ProbeResult(command="npm test", exit_code=0 if passed else 1, passed=passed, duration_ms=1, output_tail="",
                       tests_passed=9 if passed else 1, tests_total=9)


PROD = [TelemetryEvidence(source="seeded_logs", signature="s", count=5, first_seen=None, last_seen=None, sample=None,
                          related_symbol="req.param")]


@pytest.mark.parametrize(
    "ev,findings,up,tele,expected",
    [
        (event(), [finding()], probe(False), PROD, "critical"),  # tests fail and production shows it
        (event(vulnerabilities=[VulnRef(id="v", severity="critical")]), [], probe(True), [], "critical"),
        (event(vulnerabilities=[VulnRef(id="v", severity="high")]), [], None, [], "critical"),
        (event(), [finding()], probe(False), [], "high"),  # tests fail
        (event(), [finding(change_id=1)], probe(True), [], "high"),  # confirmed finding on a breaking change
        (event(), [finding(sources=("test_failure",))], None, [], "high"),
        (event(), [finding(sources=("runtime_deprecation",), change_id=2)], probe(True), [], "medium"),  # only deprecations
        (event(runtime_requirement_change=True), [], probe(True), [], "medium"),
        (event(vulnerabilities=[VulnRef(id="v", severity="medium")]), [], probe(True), [], "medium"),
        (event(), [], probe(True), [], "low"),
        (event(), [], None, [], "low"),
    ],
)
def test_severity_rules(ev, findings, up, tele, expected):
    assert severity(ev, findings, up, tele) == expected


def test_findings_the_llm_rejected_do_not_count():
    assert severity(event(), [finding(change_id=1, confirmed=False)], probe(True), []) == "low"


def test_first_rule_wins():
    ev = event(vulnerabilities=[VulnRef(id="v", severity="critical")], runtime_requirement_change=True)
    assert severity(ev, [finding(change_id=1)], probe(False), PROD) == "critical"


@pytest.mark.parametrize(
    "findings,manifest,expected",
    [
        ([], False, 1.0),  # review and deploy only
        ([], True, 2.0),  # + manifest change
        ([finding()], True, 3.0),  # .25 + .5 + 1 + 1 = 2.75 -> 3.0
        ([finding(line=i) for i in range(1, 7)], True, 4.0),  # 6 * .25 + .5 + 1 + 1 = 4.0
        ([finding(file="a.js"), finding(file="b.js", line=2)], False, 2.5),  # 2 * .25 + 2 * .5 + 1 = 2.5
        ([finding(confirmed=False)], False, 1.0),  # rejected findings cost nothing
        ([finding(line=i) for i in range(1, 4)], False, 2.5),  # .75 + .5 + 1 = 2.25 -> 2.5
    ],
)
def test_effort_formula(findings, manifest, expected):
    assert effort_hours(findings, manifest) == expected


# --- plan wording ---------------------------------------------------------------------------------------------


class FakeLLM(LLM):
    def __init__(self, conn, answers=None, error=None):
        super().__init__(conn, None, Settings(_env_file=None))
        self.answers, self.error, self.prompt = list(answers or []), error, ""

    async def chat(self, schema, *, prompt_version, messages, tier="strong"):
        self.prompt = messages[-1]["content"]
        if self.error:
            raise self.error
        return self.answers.pop(0)


@pytest.fixture
def conn(tmp_path):
    c = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(c)
    return c


def run(coro):
    return asyncio.run(coro)


def test_plan_numbers_come_from_code_and_the_prompt_carries_the_facts(conn):
    text = PlanText(rationale="Because tests fail.", steps=["Bump express.", "Fix req.param."], validation=["Tests pass."])
    llm = FakeLLM(conn, [text])
    plan = run(build_plan(llm, event(), [finding(change_id=1)], probe(False), PROD))
    assert isinstance(plan, Plan) and plan.severity == "critical" and plan.effort_hours == 3.0
    assert plan.rationale == "Because tests fail." and plan.steps == ["Bump express.", "Fix req.param."]
    for fact in ("express 4.21.2 -> 5.3.0 (major)", "Severity (computed): critical", "Effort in hours (computed): 3.0",
                 "a.js:1 req.param", "failed (1 of 9 tests pass)", "5 log lines"):
        assert fact in llm.prompt


def test_plan_falls_back_to_a_template_when_the_model_fails(conn):
    llm = FakeLLM(conn, error=LLMOutputError("bad"))
    plan = run(build_plan(llm, event(), [finding(change_id=1)], probe(False), []))
    assert plan.severity == "high" and "Upgrade express to 5.3.0." in plan.steps[0]
    assert any("a.js:1" in step for step in plan.steps) and plan.validation


def test_plan_without_a_model_uses_the_template():
    plan = run(build_plan(None, event(), [], probe(True), []))
    assert plan.severity == "low" and plan.effort_hours == 2.0


def test_manifest_flag_changes_effort():
    assert run(build_plan(None, event(), [], probe(True), [], manifest_changes=False)).effort_hours == 1.0


def test_fallback_text_is_valid():
    text = fallback_text(event(), [finding()], "high", probe(False))
    assert text.steps and text.validation and "high" in text.rationale


@pytest.mark.parametrize("sev,route", [("low", "report_only"), ("medium", "full"), ("high", "full"), ("critical", "full")])
def test_routing_after_plan(sev, route):
    plan = Plan(severity=sev, effort_hours=1, rationale="r", steps=["s"], validation=["v"])
    assert route_after_plan(plan) == route
