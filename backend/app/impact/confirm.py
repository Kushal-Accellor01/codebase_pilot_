"""LLM confirmation of findings that only a text search found (BS 7.1 step 7).

Findings backed by a runtime warning or a failing test are already evidence; the model only reviews the rest.
The fast tier is used. A finding the model rejects stays in the table with confirmed=False (never deleted).
"""
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.contracts import Finding
from app.core.llm import LLM, LLMError
from app.updates.analysis import load_prompt
from app.updates.models import ChangeRef

logger = logging.getLogger(__name__)

BATCH_SIZE = 8
CONTEXT_LINES = 15
PROMPT_VERSION = "impact_confirm_v1"


class Verdict(BaseModel):
    id: int
    affected: bool
    reason: str = Field(max_length=300)


class Verdicts(BaseModel):
    results: list[Verdict]


def needs_confirmation(finding: Finding) -> bool:
    return finding.sources == ["pattern"]


def _window(root: Path, rel: str, line: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lo, hi = max(1, line - CONTEXT_LINES), min(len(lines), line + CONTEXT_LINES)
    return "\n".join(f"{'>>' if n == line else '  '} {n:>4} {lines[n - 1]}" for n in range(lo, hi + 1))


def _item(item_id: int, finding: Finding, root: Path, changes: dict[int | None, ChangeRef]) -> str:
    change = changes.get(finding.change_id)
    about = f"{change.description} (migration: {change.migration_hint or 'none given'})" if change else "unknown"
    return (
        f"[item {item_id}] {finding.file_path}:{finding.line} symbol={finding.symbol or '-'}\n"
        f"change: {about}\ncode:\n{_window(root, finding.file_path, finding.line)}"
    )


async def confirm_findings(
    llm: LLM, findings: list[Finding], root: Path, changes: list[ChangeRef], *,
    package: str, current: str, target: str,
) -> int:
    """Set confirmed/reason on pattern-only findings in place. Returns the number of model calls made.

    Ids the model invents are ignored; ids it skips stay unconfirmed. Model errors leave findings unconfirmed.
    """
    pending = [(i, f) for i, f in enumerate(findings) if needs_confirmation(f)]
    by_id = {c.id: c for c in changes}
    system, template = load_prompt(PROMPT_VERSION)
    calls = 0
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        items = "\n\n".join(_item(i, f, root, by_id) for i, f in batch)
        user = template.format(package=package, current_version=current, target_version=target, items=items)
        try:
            out = await llm.chat(
                Verdicts, prompt_version=PROMPT_VERSION, tier="fast",
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except LLMError as exc:
            logger.warning("impact confirmation skipped for a batch: %s", exc)
            continue
        calls += 1
        allowed = {i for i, _ in batch}
        for verdict in out.results:
            if verdict.id in allowed:
                findings[verdict.id].confirmed = verdict.affected
                findings[verdict.id].reason = verdict.reason
    return calls
