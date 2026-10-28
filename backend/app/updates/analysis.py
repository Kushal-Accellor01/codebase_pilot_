"""LLM extraction of breaking changes from fetched release notes (S2 5.8) - the only LLM call in Step 2.

The model reads notes that code fetched; it is never asked what changed "from memory". Notes are untrusted:
they are wrapped in <notes> tags, the model gets no tools, and only schema-valid output is accepted. Code then
verifies every claim against the notes (evidence.py).
"""
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from functools import cache

from app.core.llm import LLM
from app.updates import store
from app.updates.config import PROMPT_VERSION, PROMPTS_DIR, SHARED_PROMPTS_DIR
from app.updates.evidence import validate_changes
from app.updates.models import ChangeRef, NotesResult, ReleaseNote, SummaryOut, UpdateAnalysisOut
from app.updates.notes import truncate_notes
from app.updates.versions import compare_versions

logger = logging.getLogger(__name__)

CHUNK_CHAR_LIMIT = 40_000
SUMMARY_PROMPT_VERSION = "summary_v1"


@dataclass
class AnalysisResult:
    analysis_id: int | None
    summary: str | None
    changes: list[ChangeRef]
    notes_quality: str
    dropped: list[dict] = field(default_factory=list)
    from_cache: bool = False


@cache
def load_prompt(name: str) -> tuple[str, str]:
    """(system, user_template) from prompts/<name>.md, which has '## SYSTEM' and '## USER' sections."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():  # step-specific prompts live next to their step; shared ones in app/prompts
        path = SHARED_PROMPTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    system, _, user = text.partition("## USER")
    return system.replace("## SYSTEM", "", 1).strip(), user.strip()


def _safe(body: str) -> str:
    # Release notes cannot close the <notes> block and smuggle instructions after it.
    return body.replace("</notes>", "<\\/notes>").replace("<notes>", "<\\notes>")


def version_block(note: ReleaseNote) -> str:
    date = (note.published_at or "")[:10] or "undated"
    return f"### {note.version} ({date}) {note.url}\n{_safe(truncate_notes(note.body_md))}"


def build_notes_text(notes: NotesResult) -> str:
    """Oldest to newest, each version under its own heading."""
    return "\n\n".join(version_block(notes.notes[v]) for v in notes.versions if v in notes.notes)


def chunk_by_major(notes: NotesResult, limit: int | None = None) -> list[list[str]]:
    """One chunk if everything fits; otherwise split on major-version boundaries (never mid-version)."""
    limit = CHUNK_CHAR_LIMIT if limit is None else limit
    versions = [v for v in notes.versions if v in notes.notes]
    if len(build_notes_text(notes)) <= limit:
        return [versions] if versions else []
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for v in versions:
        block = len(version_block(notes.notes[v]))
        # Prefer a cut at a major boundary, but never let one chunk outgrow the limit: a single major with
        # dozens of pre-release sections (Express 5) is split by size.
        starts_new_major = bool(current) and v.split(".")[0] != current[-1].split(".")[0]
        if current and size + block > limit and (starts_new_major or size > limit * 0.75):
            chunks.append(current)
            current, size = [], 0
        current.append(v)
        size += block
    if current:
        chunks.append(current)
    return chunks


def _subset(notes: NotesResult, versions: list[str]) -> NotesResult:
    return NotesResult(versions=versions, notes={v: notes.notes[v] for v in versions}, coverage=notes.coverage)


def _messages(facts: dict, notes_text: str, rejected: list[str] | None = None) -> list[dict]:
    system, user_template = load_prompt("dependency_update_v1")
    user = user_template.format(notes_by_version=notes_text, **facts)
    if rejected:
        user += (
            "\n\nYour previous answer contained evidence quotes that do NOT appear in the notes and were "
            "rejected:\n" + "\n".join(f"- {q[:200]}" for q in rejected)
            + "\nEvery evidence_quote must be copied verbatim from the notes above. Answer again."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def dedupe(changes: list[ChangeRef]) -> list[ChangeRef]:
    """Merge chunk results by (kind, symbols). Changes that name no symbol are told apart by their quote,
    otherwise every symbol-less change of one kind would collapse into one."""
    seen: set[tuple] = set()
    out = []
    for c in changes:
        key = (c.kind, tuple(sorted(c.api_symbols)), "" if c.api_symbols else " ".join(c.evidence_quote.split()))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


async def _extract_chunk(
    llm: LLM, facts: dict, chunk: NotesResult
) -> tuple[UpdateAnalysisOut, list[ChangeRef], list[dict]]:
    text = build_notes_text(chunk)
    out = await llm.chat(UpdateAnalysisOut, prompt_version=PROMPT_VERSION, messages=_messages(facts, text))
    checked = validate_changes(out.changes, chunk.notes)
    total = len(checked.kept) + len(checked.dropped)
    if checked.dropped and len(checked.dropped) * 2 > total:
        # More than half invented: ask once more, naming the rejected quotes.
        rejected = [d["evidence_quote"] for d in checked.dropped]
        out = await llm.chat(
            UpdateAnalysisOut, prompt_version=PROMPT_VERSION, messages=_messages(facts, text, rejected)
        )
        retry = validate_changes(out.changes, chunk.notes)
        checked.dropped = checked.dropped + retry.dropped
        checked.kept = retry.kept
    return out, checked.kept, checked.dropped


async def analyse_update(
    llm: LLM,
    conn: sqlite3.Connection,
    *,
    ecosystem: str,
    name: str,
    current: str,
    target: str,
    update_type: str,
    engines_current: str | None,
    engines_target: str | None,
    deprecation_message: str | None,
    notes: NotesResult,
) -> AnalysisResult:
    """Extract, validate and store the changes for one upgrade. Returns the cached analysis if it exists."""
    full_text = build_notes_text(notes)
    input_sha = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    row = store.find_analysis(conn, ecosystem, name, current, target, PROMPT_VERSION, input_sha)
    if row is not None:
        data = json.loads(row["analysis_json"])
        return AnalysisResult(
            analysis_id=row["id"],
            summary=row["summary"],
            changes=[ChangeRef(**c) for c in data["changes"]],
            notes_quality=data.get("notes_quality", "sparse"),
            dropped=json.loads(row["dropped_json"] or "[]"),
            from_cache=True,
        )

    facts = {
        "ecosystem": ecosystem, "name": name, "current_version": current, "target_version": target,
        "update_type": update_type, "engines_current": engines_current or "unknown",
        "engines_target": engines_target or "unknown", "deprecation_message": deprecation_message or "none",
    }
    changes: list[ChangeRef] = []
    dropped: list[dict] = []
    summaries: list[str] = []
    quality_rank = {"none": 0, "sparse": 1, "detailed": 2}
    quality = "none"
    for versions in chunk_by_major(notes):
        out, kept, bad = await _extract_chunk(llm, facts, _subset(notes, versions))
        changes += kept
        dropped += bad
        summaries.append(out.summary)
        if quality_rank[out.notes_quality] > quality_rank[quality]:
            quality = out.notes_quality

    changes = sorted(dedupe(changes), key=lambda c: (_version_key(c.version), not c.breaking))
    if len(summaries) > 1:
        system, user_template = load_prompt("dependency_summary_v1")
        user = user_template.format(changes_json=json.dumps([c.model_dump() for c in changes]), **{
            k: facts[k] for k in ("ecosystem", "name", "current_version", "target_version", "update_type")})
        summary = (await llm.chat(
            SummaryOut, prompt_version=SUMMARY_PROMPT_VERSION,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )).summary
    else:
        summary = summaries[0] if summaries else None

    analysis_id = store.save_analysis(
        conn, ecosystem=ecosystem, name=name, from_version=current, to_version=target,
        prompt_version=PROMPT_VERSION, model=llm.model_label("strong"), input_sha256=input_sha,
        summary=summary, notes_coverage=notes.coverage, notes_quality=quality, changes=changes, dropped=dropped,
    )
    return AnalysisResult(analysis_id, summary, changes, quality, dropped)


def _version_key(version: str):
    from functools import cmp_to_key

    return cmp_to_key(compare_versions)(version)
