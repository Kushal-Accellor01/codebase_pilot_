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
    skipped_versions: int = 0  # non-semver version strings ignored
    error: str | None = None


class PreflightReport(BaseModel):
    scan_id: str
    dependencies: int
    declared_without_constraint: int
    missing_dependency_type: int
    lockfiles: int
    warnings: list[str] = Field(default_factory=list)
