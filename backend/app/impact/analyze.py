"""Step 3: impact analysis for one upgrade run (BS 7.1).

Package scope from the graph -> pattern search -> runtime-deprecation probe on the current version ->
upgrade-and-test probe on the target -> merge -> LLM confirmation of pattern-only lines.
"""
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.core import events
from app.core.contracts import DependencyChangeEvent, Finding, ProbeResult
from app.core.llm import LLM
from app.core.workspace import ExecNotAllowed
from app.impact import probes
from app.impact import store as impact_store
from app.impact.confirm import confirm_findings
from app.impact.merge import merge_findings
from app.impact.patterns import CODE_EXTENSIONS, search_scope

logger = logging.getLogger(__name__)

IGNORED_DIRS = {"node_modules", ".git", "dist", "build", "coverage", "workspace"}


@dataclass
class ImpactResult:
    findings: list[Finding]
    scope_files: list[str]
    baseline: ProbeResult | None = None
    upgrade_probe: ProbeResult | None = None
    baseline_output: str = ""
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0


def all_code_files(root: Path, cap: int = impact_store.SCOPE_CAP) -> list[str]:
    """Fallback scope for packages the graph can't link to files (e.g. loaded by name, like a view engine)."""
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in rel.parts) or path.suffix not in CODE_EXTENSIONS or not path.is_file():
            continue
        found.append(rel.as_posix())
        if len(found) >= cap:
            break
    return found


async def analyse_impact(
    conn: sqlite3.Connection,
    *,
    event: DependencyChangeEvent,
    dep_node_id: str,
    root: Path,
    repo: str | None,
    run_id: str | None = None,
    llm: LLM | None = None,
    run_probes: bool = True,
    test_args: list[str] | None = None,
) -> ImpactResult:
    """`root` is the run's worktree. Probes need `repo` to be in EXEC_ALLOWED_REPOS; without it the
    analysis falls back to static signals (scope + patterns) and says so in `notes`."""

    def trace(node: str, summary: str, **data) -> None:
        if run_id:
            events.emit(conn, run_id, node, "finished", summary, data or None)

    scope = [p for p, _depth in impact_store.package_scope(conn, event.scan_id, dep_node_id)]
    notes: list[str] = []
    if len(scope) >= impact_store.SCOPE_CAP:
        scope = scope[: impact_store.SCOPE_CAP]
        notes.append(f"scope capped at {impact_store.SCOPE_CAP} files")
    if not scope:
        scope = all_code_files(root)
        notes.append("package not linked to any file in the graph: searched all code files")
    trace("scope", f"{len(scope)} files in scope", files=len(scope))

    hits = search_scope(root, scope, event.changes)
    trace("patterns", f"{len(hits)} candidate lines", hits=len(hits))

    deprecations: list[probes.Deprecation] = []
    frames: list[probes.Frame] = []
    baseline = upgrade = None
    baseline_output = ""
    if run_probes:
        try:
            base = probes.run_baseline(root, repo, test_args)
            baseline, baseline_output = base.probe, base.output
            if base.install_ok:
                deprecations = probes.parse_deprecations(base.output, str(root))
            else:
                notes.append("baseline install failed; no runtime deprecation evidence")
            trace("baseline_probe", f"{len(deprecations)} runtime deprecation warnings",
                  passed=base.probe.passed, tests_passed=base.probe.tests_passed,
                  tests_total=base.probe.tests_total)

            if event.target_version and event.package:
                up = probes.run_upgrade_probe(root, repo, event.package, event.target_version, test_args)
                upgrade = up.probe
                if up.install_ok:
                    frames = probes.parse_failure_frames(up.output, str(root))
                else:
                    notes.append("target version failed to install")
                trace("upgrade_probe", f"tests {'pass' if up.probe.passed else 'fail'} on {event.target_version}",
                      passed=up.probe.passed, tests_passed=up.probe.tests_passed, tests_total=up.probe.tests_total)
        except ExecNotAllowed as exc:
            notes.append(str(exc))

    findings = merge_findings(root, hits, deprecations, frames, event.changes)
    calls = 0
    if llm is not None:
        calls = await confirm_findings(
            llm, findings, root, event.changes, package=event.package,
            current=event.current_version or "?", target=event.target_version or "?",
        )
        trace("confirm", f"{sum(1 for f in findings if f.confirmed is not None)} lines reviewed")
    return ImpactResult(findings, scope, baseline, upgrade, baseline_output, notes, calls)
