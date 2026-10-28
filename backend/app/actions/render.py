"""Pull request and issue bodies. Pure functions: same input, same text (snapshot-tested)."""
from dataclasses import dataclass, field

from app.core.contracts import DependencyChangeEvent, Finding, Plan, ProbeResult, TelemetryEvidence

SOURCE_LABELS = {
    "pattern": "code search",
    "runtime_deprecation": "runtime warning",
    "test_failure": "failing test",
    "llm": "model review",
}


def marker(run_id: str) -> str:
    return f"<!-- codebase-pilot:run={run_id} -->"


@dataclass
class BodyData:
    run_id: str
    event: DependencyChangeEvent
    plan: Plan
    findings: list[Finding]
    telemetry: list[TelemetryEvidence]
    baseline: ProbeResult | None
    unpatched: ProbeResult | None
    patched: ProbeResult | None
    approver: str
    approved_at: str
    repo_slug: str | None = None  # 'org/repo' for blob links; None for local repositories
    branch: str | None = None
    owners: dict[str, list[str]] = field(default_factory=dict)  # file path -> owners
    diff_stat: str | None = None
    failure_tail: str | None = None  # issues: the test output that blocked the fix


def _probe(probe: ProbeResult | None) -> str:
    if probe is None:
        return "not run"
    counts = f" ({probe.tests_passed} of {probe.tests_total} tests pass)" if probe.tests_total else ""
    if probe.timed_out:
        return "timed out"
    return ("passed" if probe.passed else "failed") + counts


def _cell(text: object) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def _link(data: BodyData, finding: Finding) -> str:
    label = f"{finding.file_path}:{finding.line}"
    if data.repo_slug and data.branch:
        return f"[{label}](https://github.com/{data.repo_slug}/blob/{data.branch}/{finding.file_path}#L{finding.line})"
    return f"`{label}`"


def title(event: DependencyChangeEvent) -> str:
    return f"Upgrade {event.package} {event.current_version} → {event.target_version} (Codebase Pilot)"


def render_body(data: BodyData, *, kind: str = "pull_request") -> str:
    e = data.event
    out: list[str] = [marker(data.run_id), ""]
    if kind == "issue":
        out += [
            f"Pilot could not produce a passing fix for **{e.package} {e.current_version} → {e.target_version}**, "
            "so an engineer needs to take over. Everything it found is below.", "",
        ]
    elif e.summary:
        out += [e.summary, ""]
    out += [
        f"**Severity:** {data.plan.severity} · **Estimated effort:** about {data.plan.effort_hours:g} h", "",
        data.plan.rationale, "",
    ]

    out += ["### What changed in the new version", ""]
    if e.changes:
        out += ["| Change | Breaking | Version | Evidence |", "| --- | --- | --- | --- |"]
        for c in e.changes:
            symbols = f" (`{'`, `'.join(c.api_symbols)}`)" if c.api_symbols else ""
            out.append(
                f"| {_cell(c.description)}{symbols} | {'yes' if c.breaking else 'no'} | {c.version} | "
                f"[release notes]({c.evidence_url}): “{_cell(c.evidence_quote)}” |"
            )
    else:
        out.append("No change list was extracted from the release notes.")
    out.append("")

    live = [f for f in data.findings if f.confirmed is not False]
    out += [f"### Where our code is affected ({len(live)} lines)", ""]
    if live:
        for f in live:
            how = ", ".join(SOURCE_LABELS.get(s, s) for s in f.sources)
            owner = f" · owner {' '.join(data.owners[f.file_path])}" if data.owners.get(f.file_path) else ""
            what = f"`{f.symbol}` " if f.symbol else ""
            out.append(f"- {_link(data, f)} {what}(found by {how}){owner}")
    else:
        out.append("No affected lines were found.")
    out.append("")

    out += [
        "### Tests", "",
        "| Run | Result |", "| --- | --- |",
        f"| Current version | {_probe(data.baseline)} |",
        f"| New version, no code changes | {_probe(data.unpatched)} |",
        f"| New version, with this fix | {_probe(data.patched)} |", "",
    ]
    if kind == "issue" and data.failure_tail:
        out += ["<details><summary>Failing test output</summary>", "", "```", data.failure_tail[-3000:], "```", "",
                "</details>", ""]

    if data.telemetry:
        out += ["### Seen in production", ""]
        for t in data.telemetry:
            out.append(f"- {t.count} log lines, last seen {t.last_seen or 'unknown'}: `{_cell(t.signature)[:140]}`")
        out.append("")

    if data.plan.steps:
        out += ["### Plan", ""] + [f"{i}. {step}" for i, step in enumerate(data.plan.steps, 1)] + [""]
    if kind == "pull_request" and data.diff_stat:
        out += ["<details><summary>Changed files</summary>", "", "```", data.diff_stat, "```", "", "</details>", ""]

    out += ["---", f"Approved by {data.approver} on {data.approved_at}. Run `{data.run_id}`. "
            "Nothing was merged automatically."]
    return "\n".join(out) + "\n"
