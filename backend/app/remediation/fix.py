"""Step 6: produce a patch. Codemod if one is registered, then LLM edits, then the manifest bump (BS 7.5).

The diff always comes from git, never from the model. Validation (running the tests) is separate so the
graph can retry the loop with the failure output.
"""
import logging
from pathlib import Path

from pydantic import BaseModel

from app.core import workspace
from app.core.contracts import DependencyChangeEvent, Edit, Finding, PatchResult, ProbeResult
from app.core.llm import LLM, LLMError
from app.impact.probes import to_probe
from app.remediation import codemods
from app.remediation.edits import ApplyResult, apply_edits
from app.updates.analysis import load_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "fix_v1"
CONTEXT_LINES = 20


class FixEdits(BaseModel):
    edits: list[Edit]


def _window(root: Path, rel: str, line: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lo, hi = max(1, line - CONTEXT_LINES), min(len(lines), line + CONTEXT_LINES)
    return "\n".join(f"{'>>' if n == line else '  '} {n:>4} {lines[n - 1]}" for n in range(lo, hi + 1))


def remaining_findings(root: Path, findings: list[Finding]) -> list[Finding]:
    """Findings whose flagged text is still in the file (earlier attempts may have fixed some)."""
    out = []
    for f in findings:
        if f.confirmed is False:
            continue
        try:
            text = (root / f.file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f.snippet and f.snippet in text:
            out.append(f)
    return out


def _prompt(event: DependencyChangeEvent, root: Path, findings: list[Finding], failure_tail: str | None) -> str:
    _, template = load_prompt(PROMPT_VERSION)
    by_id = {c.id: c for c in event.changes}
    changes = "\n".join(
        f"- {c.description} (migration: {c.migration_hint or 'none given'}) | evidence: \"{c.evidence_quote}\""
        for c in event.changes if c.breaking
    ) or "none"
    code = "\n\n".join(
        f"{f.file_path}:{f.line} [{f.symbol or 'affected line'}]"
        + (f" (change: {by_id[f.change_id].description})" if f.change_id in by_id else "")
        + f"\n{_window(root, f.file_path, f.line)}"
        for f in findings
    )
    failure = ""
    if failure_tail:
        failure = (
            "\nThe previous attempt failed these tests (output tail):\n"
            f"<test_output>\n{failure_tail[-3000:]}\n</test_output>\n"
        )
    return template.format(
        package=event.package, current_version=event.current_version, target_version=event.target_version,
        changes=changes, code=code, failure=failure,
    )


async def propose_and_apply(
    llm: LLM, event: DependencyChangeEvent, root: Path, findings: list[Finding], scope_files: list[str],
    failure_tail: str | None = None,
) -> ApplyResult:
    todo = remaining_findings(root, findings)
    if not todo:
        return ApplyResult()
    system, _ = load_prompt(PROMPT_VERSION)
    try:
        out = await llm.chat(
            FixEdits, prompt_version=PROMPT_VERSION,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": _prompt(event, root, todo, failure_tail)}],
        )
    except LLMError as exc:
        logger.warning("fix edits unavailable: %s", exc)
        result = ApplyResult()
        result.rejected.append({"file": "", "find": "", "replace": "", "reason": f"model error: {exc}"})
        return result
    return apply_edits(root, out.edits, scope_files)


def bump_manifest(root: Path, repo: str | None, package: str, target: str) -> tuple[bool, str]:
    """`npm install pkg@target --save-exact --ignore-scripts`; returns (ok, output tail)."""
    result = workspace.npm_install_exact(root, repo, package, target)
    return result.passed, result.output_tail


def manifest_diff(root: Path) -> str | None:
    """package.json / lockfile part of the diff, or None when the manifest is unchanged."""
    result = workspace.run(["git", "-C", str(root), "diff", "--no-color", "HEAD", "--", "package.json"], root,
                           timeout_s=workspace.GIT_TIMEOUT_S)
    return result.output.strip() or None if result.passed else None


async def make_patch(
    llm: LLM | None,
    event: DependencyChangeEvent,
    root: Path,
    repo: str | None,
    findings: list[Finding],
    scope_files: list[str],
    *,
    attempt: int,
    failure_tail: str | None = None,
    bump: bool = True,
) -> PatchResult:
    method = "llm_edits"
    applied: list[Edit] = []
    rejected: list[dict] = []

    codemod = codemods.find_codemod(event.package, event.current_version or "", event.target_version or "")
    if codemod and attempt == 1:
        ran = codemods.run_codemod(root, repo, codemod)
        method = "codemod"
        if not ran.passed:
            reason = f"codemod failed: {ran.output_tail[-300:]}"
            rejected.append({"file": "", "find": "", "replace": "", "reason": reason})

    if llm is not None:
        result = await propose_and_apply(llm, event, root, findings, scope_files, failure_tail)
        applied += result.applied
        rejected += result.rejected
        if codemod and result.applied:
            method = "codemod+llm"

    if bump and event.target_version:
        ok, tail = bump_manifest(root, repo, event.package, event.target_version)
        if not ok:
            reason = f"version bump failed: {tail[-300:]}"
            rejected.append({"file": "package.json", "find": "", "replace": "", "reason": reason})

    return PatchResult(
        attempt=attempt, method=method, edits=applied, rejected=rejected, diff=workspace.diff(root),
        manifest_change=manifest_diff(root),
    )


def validate(root: Path, repo: str | None, test_args: list[str] | None = None) -> ProbeResult:
    """Run the repository's tests (its configured test command) on the patched worktree."""
    return to_probe(workspace.run_tests(root, repo, test_args))
