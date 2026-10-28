"""Evidence validation (S2 5.9): every LLM claim must be backed by a verbatim quote in the fetched notes.

Code, not the model, decides what is kept. `evidence_url` is always taken from the release-notes row of the
matched version; the model never supplies a URL.
"""
from dataclasses import dataclass

from app.updates.models import ChangeItem, ChangeRef, ReleaseNote


def _norm(s: str) -> str:
    return " ".join(s.replace("`", "").split()).casefold()


def quote_in_notes(quote: str, notes: str) -> bool:
    q = _norm(quote).strip(" .*-")
    return len(q) >= 12 and q in _norm(notes)


def symbol_in_notes(symbol: str, notes: str) -> bool:
    s = _norm(symbol)
    return bool(s) and s in _norm(notes)


@dataclass
class ValidationResult:
    kept: list[ChangeRef]
    dropped: list[dict]


def validate_changes(changes: list[ChangeItem], notes: dict[str, ReleaseNote]) -> ValidationResult:
    """Keep the changes whose quote is found; drop (and record) the rest.

    1. The quote must appear in the notes of its stated `version`. If not, it must appear in exactly one
       other version of the range, and the version is corrected to that one.
    2. Each api_symbol must appear in that version's notes; symbols that don't are removed.
    3. A change whose quote can't be placed is dropped.
    """
    kept: list[ChangeRef] = []
    dropped: list[dict] = []
    for change in changes:
        version = change.version
        note = notes.get(version)
        if note is None or not quote_in_notes(change.evidence_quote, note.body_md):
            holders = [v for v, n in notes.items() if quote_in_notes(change.evidence_quote, n.body_md)]
            if len(holders) != 1:
                reason = "quote not found in any version" if not holders else "quote found in several versions"
                dropped.append({**change.model_dump(), "drop_reason": reason})
                continue
            version = holders[0]
            note = notes[version]
        symbols = [s for s in change.api_symbols if symbol_in_notes(s, note.body_md)]
        kept.append(
            ChangeRef(
                kind=change.kind,
                breaking=change.breaking,
                api_symbols=symbols,
                description=change.description,
                migration_hint=change.migration_hint,
                version=version,
                evidence_url=note.url,
                evidence_quote=change.evidence_quote,
            )
        )
    return ValidationResult(kept=kept, dropped=dropped)
