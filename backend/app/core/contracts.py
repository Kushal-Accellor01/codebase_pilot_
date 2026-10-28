"""Shared contracts (BS section 6). Change only through a reviewed PR.

DependencyChangeEvent and its parts are defined by Step 2 (updates.models) and re-exported here.
"""
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from app.updates.models import ChangeRef, DependencyChangeEvent, ImportingFile, VulnRef

Source = Literal["pattern", "runtime_deprecation", "test_failure", "llm"]


class Finding(BaseModel):
    file_path: str
    line: int
    end_line: int | None = None
    snippet: str
    symbol: str | None = None
    change_id: int | None = None  # update_changes.id it relates to
    sources: list[Source]  # every signal that found this line
    confidence: float  # computed (BS 7.1 step 6)
    confirmed: bool | None = None  # LLM verdict
    reason: str = ""


class ProbeResult(BaseModel):
    command: str
    exit_code: int | None
    passed: bool
    duration_ms: int
    output_tail: str
    timed_out: bool = False
    tests_passed: int | None = None
    tests_total: int | None = None


class TelemetryEvidence(BaseModel):
    source: str
    signature: str
    count: int
    first_seen: str | None
    last_seen: str | None
    sample: str | None
    related_symbol: str | None


class Plan(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    effort_hours: float
    rationale: str
    steps: list[str]
    validation: list[str]


class Edit(BaseModel):
    file: str  # repo-relative POSIX path
    find: str  # exact text, must occur exactly once in the file
    replace: str


class PatchResult(BaseModel):
    attempt: int
    method: Literal["codemod", "llm_edits", "codemod+llm"]
    edits: list[Edit]
    rejected: list[dict] = Field(default_factory=list)
    diff: str
    manifest_change: str | None
    validation: ProbeResult | None = None


class UpgradeState(TypedDict, total=False):
    run_id: str
    event: dict  # DependencyChangeEvent.model_dump()
    worktree: str  # absolute path of this run's git worktree
    scope_files: list[str]
    findings: list[dict]  # Finding.model_dump()
    baseline: dict  # ProbeResult on the current version
    upgrade_probe: dict  # ProbeResult on the target version, no code changes
    telemetry: list[dict]
    plan: dict
    patches: list[dict]  # PatchResult.model_dump(), one per attempt
    attempts: int
    decision: Literal["approved", "rejected"] | None
    decided_by: str | None
    links: list[str]
    route: str  # 'full' | 'report_only' | 'needs_engineer'
    # Added with the orchestrator (B1): small values only; big data lives in tables.
    repo: str | None  # repository identity: 'org/repo' or a local path
    clone: str  # path of the kept clone of the scanned commit
    config: dict  # RepoPilotConfig.model_dump()
    exec_allowed: bool  # may the repository's code be run (EXEC_ALLOWED_REPOS)
    failure_tail: str | None  # test output of the last failed attempt, fed to the next one
    severity: str
    findings_count: int
    telemetry_count: int
    patch: dict  # latest PatchResult.model_dump()
    pr: dict  # what act published


__all__ = [
    "ChangeRef", "DependencyChangeEvent", "Edit", "Finding", "ImportingFile", "PatchResult", "Plan",
    "ProbeResult", "Source", "TelemetryEvidence", "UpgradeState", "VulnRef",
]
