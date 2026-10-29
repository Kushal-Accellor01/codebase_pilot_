"""Response and request models of the HTTP API. FastAPI turns these into the OpenAPI document the frontend
types are generated from (`npm run gen:types`)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

Risk = Literal["critical", "high", "medium", "low", "none"]
Reachability = Literal["reachable_in_code", "imported", "declared_only"]


class Health(BaseModel):
    status: str
    migrations: list[str]
    llm_mode: str
    jobs_queued: int
    embedded_worker: bool = False


class ScanOut(BaseModel):
    id: str
    repo: str
    repo_url: str | None = None
    commit_sha: str | None = None
    scanned_at: str | None = None
    has_nodes: bool
    dependencies: int
    analysed: int
    updates: int
    critical: int


class DependencyRow(BaseModel):
    id: int
    name: str
    ecosystem: str
    dependency_type: str
    declared_constraint: str | None = None
    current_version: str | None = None
    latest_in_range: str | None = None
    latest_version: str | None = None
    recommended_target: str | None = None
    update_type: str | None = None
    requires_manifest_change: bool
    vuln_count: int
    max_severity: str | None = None
    is_deprecated: bool
    importer_count: int
    risk_level: Risk
    priority_score: float
    status: str
    error: str | None = None
    reachability: Reachability
    run_id: str | None = None
    run_status: str | None = None


class BoardItem(BaseModel):
    id: int
    run_id: str | None = None
    package: str
    type: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    kind: str
    risk: Risk
    why: str
    why_bold: str
    status: str
    status_label: str
    pill: str
    effort_hours: float | None = None
    tests_pass: bool = False

    model_config = {"populate_by_name": True}


class WaitingItem(BoardItem):
    detail: str = ""


class SecurityItem(BaseModel):
    package: str
    version: str | None = None
    count: int
    severity: str | None = None
    fixed_in: str | None = None


class AffectedFile(BaseModel):
    path: str
    lines: int
    packages: list[str]
    strongest_evidence: str


class Overview(BaseModel):
    scan_id: str
    headline: str
    lede: str
    risk: dict[str, int]
    total: int
    upgrades: list[BoardItem]
    waiting: list[WaitingItem]
    security: list[SecurityItem]
    affected_files: list[AffectedFile]
    approvals_waiting: int


class UpgradeSummary(BaseModel):
    id: str
    scan_id: str
    package: str
    ecosystem: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    status: str
    status_label: str
    pill: str
    risk: Risk
    current_node: str | None = None
    error: str | None = None
    effort_hours: float | None = None
    created_at: str
    updated_at: str

    model_config = {"populate_by_name": True}


class UpgradeDetail(BaseModel):
    run: UpgradeSummary
    event: dict[str, Any]
    findings: list[dict[str, Any]]
    telemetry: list[dict[str, Any]]
    plan: dict[str, Any] | None = None
    patches: list[dict[str, Any]]
    tests: dict[str, Any]
    evidence: dict[str, int]
    approval: dict[str, Any] | None = None
    pull_request: dict[str, Any] | None = None
    decision_pending: bool = False
    can_decide: bool
    needs_engineer: bool


class GraphNode(BaseModel):
    id: str
    kind: Literal["file", "dependency", "team"]
    label: str
    status: Literal["target", "affected", "blast_radius", "normal"]
    findings: int
    owner: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: Literal["imports", "resolves_to", "owns"]


class Graph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    capped: bool = False


# --- requests ------------------------------------------------------------------------------------------------


class ScanScope(BaseModel):
    """What a scan should look for. Everything is on by default, which is what the CLI and the webhook do."""

    vulnerabilities: bool = True
    breaking_changes: bool = True
    internal_contracts: bool = True
    production_evidence: bool = True
    fixes: bool = True
    sbom: bool = True


class ScanRequest(BaseModel):
    repo_url: str = Field(min_length=1)
    only: list[str] | None = None
    branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}$")
    scope: ScanScope | None = None


class JobAccepted(BaseModel):
    job_id: str
    scan_request_id: str | None = None


class RepoCheckRequest(BaseModel):
    repo_url: str = Field(min_length=1)


class RepoCheck(BaseModel):
    provider: str  # github | bitbucket | gitlab | local | other
    repo: str
    allowed: bool
    ok: bool
    detail: str
    branches: list[str] = []


class ScanStage(BaseModel):
    stage: str
    status: str  # queued | running | done | skipped | error
    summary: str | None = None


class ScanFound(BaseModel):
    packages: int
    declared: int
    imports: int
    internal_contracts: int


class ScanRequestOut(BaseModel):
    id: str
    repo: str
    repo_url: str
    branch: str | None = None
    status: str  # queued | running | complete | failed | cancelled
    scan_id: str | None = None
    error: str | None = None
    scope: ScanScope
    stages: list[ScanStage]
    found: ScanFound
    steps_done: int
    steps_total: int


class Vulnerability(BaseModel):
    vuln_id: str
    aliases: list[str]
    severity: str
    summary: str | None = None
    name: str
    version: str
    path: list[str]
    scope: str  # runtime | dev | optional | peer
    direct: bool
    fixed_in: str | None = None
    verdict: str  # applies | does_not_apply | not_shipped | undetermined
    reason: str
    sources: list[str]
    url: str | None = None
    repo: str | None = None
    scan_id: str | None = None


class VulnerabilityCounts(BaseModel):
    all: int
    applies: int
    runtime: int
    development: int
    transitive: int


class VulnerabilityList(BaseModel):
    scan_id: str | None = None
    packages: int
    counts: VulnerabilityCounts
    rows: list[Vulnerability]


class ScanSummary(BaseModel):
    scan_id: str
    repo: str
    repo_url: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    scanned_at: str | None = None
    packages: int
    declared: int
    advisories: int
    advisories_apply: int
    needs_decision: int
    internal_contracts: int
    sources_to_confirm: int
    has_sbom: bool
    scan_request_id: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=1000)


class DecisionAccepted(BaseModel):
    status: str


class FeedbackRequest(BaseModel):
    target_kind: Literal["impact_finding", "update_change"]
    target_id: int
    verdict: Literal["correct", "not_relevant"]
    comment: str | None = Field(default=None, max_length=1000)
    user_name: str | None = None


class FeedbackSaved(BaseModel):
    id: int


class SourceOverride(BaseModel):
    ecosystem: str = "npm"
    name: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    directory: str | None = None
