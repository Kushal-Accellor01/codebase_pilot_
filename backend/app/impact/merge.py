"""Merge every signal about a line into one Finding (BS 7.1 step 6)."""
from pathlib import Path

from app.core.contracts import Finding
from app.impact.patterns import Hit, normalise_symbol
from app.impact.probes import Deprecation, Frame
from app.updates.models import ChangeRef

CONFIDENCE = {"runtime_deprecation": 1.0, "test_failure": 0.9}
EXTRA_SOURCE_BONUS = 0.1


def _line_text(root: Path, rel: str, line: int) -> str:
    try:
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return lines[line - 1].strip()[:300] if 0 < line <= len(lines) else ""


def _change_for_symbol(symbol: str | None, changes: list[ChangeRef]) -> int | None:
    """update_changes.id of the first change that names `symbol` (or its method)."""
    if not symbol:
        return None
    method = symbol.split(".")[-1]
    for change in changes:
        names = {normalise_symbol(s) for s in change.api_symbols}
        if symbol in names or any(n.endswith("." + method) and n.split(".")[-1] == method for n in names):
            return change.id
    return None


def merge_findings(
    root: Path,
    hits: list[Hit],
    deprecations: list[Deprecation],
    frames: list[Frame],
    changes: list[ChangeRef],
) -> list[Finding]:
    by_line: dict[tuple[str, int], dict] = {}

    def entry(path: str, line: int) -> dict:
        return by_line.setdefault((path, line), {"sources": {}, "symbol": None, "snippet": "", "change_id": None})

    for hit in hits:
        e = entry(hit.file_path, hit.line)
        e["sources"]["pattern"] = max(e["sources"].get("pattern", 0), hit.confidence)
        e["symbol"] = e["symbol"] or hit.symbol
        e["snippet"] = e["snippet"] or hit.snippet
        if e["change_id"] is None:
            e["change_id"] = changes[hit.change_index].id if hit.change_index < len(changes) else None
    for dep in deprecations:
        e = entry(dep.file_path, dep.line)
        e["sources"]["runtime_deprecation"] = CONFIDENCE["runtime_deprecation"]
        e["symbol"] = e["symbol"] or dep.symbol
        if e["change_id"] is None:
            e["change_id"] = _change_for_symbol(dep.symbol, changes)
    for frame in frames:
        e = entry(frame.file_path, frame.line)
        e["sources"]["test_failure"] = CONFIDENCE["test_failure"]

    findings = []
    for (path, line), e in sorted(by_line.items()):
        confidence = min(1.0, max(e["sources"].values()) + EXTRA_SOURCE_BONUS * (len(e["sources"]) - 1))
        symbol = e["symbol"]
        findings.append(
            Finding(
                file_path=path, line=line, snippet=e["snippet"] or _line_text(root, path, line),
                symbol=symbol,
                change_id=e["change_id"], sources=list(e["sources"]), confidence=round(confidence, 2),
            )
        )
    return findings
