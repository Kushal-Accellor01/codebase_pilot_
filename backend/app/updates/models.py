from typing import Literal

from pydantic import BaseModel, Field


class ImportingFile(BaseModel):
    path: str  # POSIX
    lines: list[int] = Field(default_factory=list)  # empty if ingestion did not record lines


class Dependency(BaseModel):
    dep_node_id: str
    ecosystem: str  # as written by ingestion: 'npm', 'PyPI', ...
    name: str
    manifest_file_id: str  # raw edge source id; '' if the dependency has no DECLARES edge
    manifest_path: str  # POSIX path of the manifest, '' if unknown
    declared_constraint: str | None
    dependency_type: str = "unknown"
    marker: str | None = None
    importer_count: int = 0
    importing_files: list[ImportingFile] = Field(default_factory=list)


class UpdateVerdict(BaseModel):
    """Deterministic version facts for one dependency (Step 3 of the pipeline)."""

    current_version: str | None = None
    current_version_basis: str = "unresolvable"  # lockfile | exact_pin | max_satisfying_at_scan | unresolvable
    latest_in_range: str | None = None
    latest_version: str | None = None
    update_type: str = "unknown"  # none | patch | minor | major | unknown
    requires_manifest_change: bool = False
    versions_behind: int | None = None
    runtime_requirement_change: bool = False
    engines_current: str | None = None
    engines_target: str | None = None
    is_deprecated: bool = False
    deprecation_message: str | None = None
    latest_deprecated: bool = False  # the package's latest release is deprecated: nothing safe to move to
    skipped_versions: int = 0  # non-semver version strings ignored
    error: str | None = None
    error_kind: str | None = None  # 'invalid_declaration' (not a registry range) | 'unresolvable'


class PreflightReport(BaseModel):
    scan_id: str
    dependencies: int
    declared_without_constraint: int
    missing_dependency_type: int
    lockfiles: int
    warnings: list[str] = Field(default_factory=list)


class UpdateRow(BaseModel):
    """One dependency_updates row, as written by the pipeline."""

    scan_id: str
    dep_node_id: str
    ecosystem: str
    name: str
    manifest_file_id: str = ""
    dependency_type: str = "unknown"
    declared_constraint: str | None = None
    current_version: str | None = None
    current_version_basis: str | None = None
    latest_in_range: str | None = None
    latest_version: str | None = None
    recommended_target: str | None = None
    update_type: str | None = None
    requires_manifest_change: bool = False
    versions_behind: int | None = None
    runtime_requirement_change: bool = False
    is_deprecated: bool = False
    deprecation_message: str | None = None
    vuln_count: int = 0
    max_severity: str | None = None
    min_safe_version: str | None = None
    importer_count: int = 0
    priority_score: float | None = None
    risk_level: str | None = None
    status: str = "pending"
    error: str | None = None


class RunStats(BaseModel):
    deps_total: int = 0
    up_to_date: int = 0
    updates: dict[str, int] = Field(default_factory=lambda: {"patch": 0, "minor": 0, "major": 0})
    vulnerable: int = 0
    deprecated: int = 0
    analysed: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    notes_unavailable: int = 0
    source_unknown: int = 0
    errors: int = 0
    undeclared_imports: int = 0
    statuses: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)  # non-fatal problems; the run is marked 'partial'
    duration_s: float = 0.0


class VulnRecord(BaseModel):
    """An OSV advisory as cached in the `vulnerabilities` table."""

    id: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    severity: str = "unknown"  # critical | high | medium | low | unknown
    cvss_vector: str | None = None
    published_at: str | None = None
    modified_at: str | None = None
    url: str
    raw: dict


class SourceRepo(BaseModel):
    """Where a package's code and release notes live (a verified GitHub repository, or 'unknown')."""

    owner: str
    repo: str
    directory: str | None = None
    default_branch: str | None = None
    archived: bool = False
    changelog_path: str | None = None
    tag_format: str | None = None
    method: str = "registry"  # override | registry | deps_dev | homepage | manual
    confidence: str = "high"  # high | medium | low
    verified: bool = False
    status: str = "resolved"  # resolved | unknown

    @property
    def slug(self) -> str:
        return f"github.com/{self.owner}/{self.repo}"


class ReleaseNote(BaseModel):
    """Release notes of one version, from a GitHub release, a changelog file, or a commit list."""

    version: str
    source: str  # github_release | changelog_file | compare_commits
    url: str
    published_at: str | None = None
    body_md: str


class NotesResult(BaseModel):
    versions: list[str]  # every stable version in (current, target], oldest first
    notes: dict[str, ReleaseNote]  # version -> note, only versions that have notes
    coverage: float = 0.0  # versions with notes / versions in range

    @property
    def available(self) -> bool:
        return bool(self.notes)


# --- Output contract to the impact-analysis step (S2 section 9) --------------------------------------------


class VulnRef(BaseModel):
    id: str
    severity: str
    summary: str | None = None
    fixed_in: str | None = None
    url: str | None = None


class ChangeRef(BaseModel):
    id: int | None = None  # update_changes.id, so findings can point at the change they come from
    kind: str
    breaking: bool
    api_symbols: list[str]
    description: str
    migration_hint: str | None = None
    version: str
    evidence_url: str
    evidence_quote: str


class DependencyChangeEvent(BaseModel):
    event_id: str  # f"{scan_id}:{ecosystem}:{name}:{current}->{target}"
    scan_id: str
    ecosystem: str
    package: str
    dependency_type: str
    manifest_path: str  # POSIX, e.g. "package.json"
    declared_constraint: str | None = None
    current_version: str | None = None
    current_version_basis: str
    target_version: str | None = None  # recommended_target
    latest_version: str | None = None
    update_type: str
    requires_manifest_change: bool
    runtime_requirement_change: bool
    is_deprecated: bool
    deprecation_message: str | None = None
    vulnerabilities: list[VulnRef] = Field(default_factory=list)
    changes: list[ChangeRef] = Field(default_factory=list)  # only evidence-validated changes
    summary: str | None = None
    notes_coverage: float | None = None
    source_repo: str | None = None  # "github.com/owner/repo"
    importer_count: int
    importing_files: list[ImportingFile] = Field(default_factory=list)
    priority_score: float
    risk_level: str
    status: str


# --- LLM output schema (S2 7.3) ----------------------------------------------------------------------------

ChangeKind = Literal[
    "removed", "renamed", "signature_changed", "behavior_changed", "deprecated",
    "security", "runtime_requirement", "new_feature", "bug_fix", "other",
]


class ChangeItem(BaseModel):
    kind: ChangeKind
    breaking: bool
    api_symbols: list[str] = Field(default_factory=list)
    description: str = Field(max_length=400)
    migration_hint: str | None = Field(default=None, max_length=400)
    version: str
    evidence_quote: str = Field(max_length=300)


class UpdateAnalysisOut(BaseModel):
    summary: str = Field(max_length=800)
    changes: list[ChangeItem]
    notes_quality: Literal["detailed", "sparse", "none"]


class SummaryOut(BaseModel):
    summary: str = Field(max_length=800)
