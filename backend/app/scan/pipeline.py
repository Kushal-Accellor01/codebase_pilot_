"""S2: the stages of a scan, reported one by one so the progress page can show them.

A scan request has a scope (what to look for) and nine stages:

    clone, layer1, sbom, internal, layer2, vulnerabilities, versions, changes, impact

`scan` and `dependency_updates` jobs call into here. A stage that is switched off or not built says so
(`skipped` with the reason); a stage that fails records `error` and the next stage still runs (ground rule 6).
"""
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.config import Settings
from app.orchestrator import store as run_store
from app.scan import sbom as sbom_module
from app.scan import store as scan_store
from app.scan import vulns as vulns_module
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)

SCOPE_KEYS = ("vulnerabilities", "breaking_changes", "internal_contracts", "production_evidence", "fixes", "sbom")
ACTIVE_RUN_STATUSES = ("queued", "analyzing", "fixing", "validating", "approved")


def full_scope() -> dict[str, bool]:
    """Everything on: what a scan without a request (CLI, webhook, schedule) has always done."""
    return dict.fromkeys(SCOPE_KEYS, True)


def scope_of(raw: dict | None) -> dict[str, bool]:
    """A complete scope from a partial one; unknown keys are dropped, missing keys are on."""
    scope = full_scope()
    for key in SCOPE_KEYS:
        if raw and key in raw:
            scope[key] = bool(raw[key])
    return scope


# Stage a person may retry -> every stage that retry re-runs (the rest keep their result)
RETRY_STAGES: dict[str, frozenset[str]] = {
    "sbom": frozenset({"sbom", "vulnerabilities"}),
    "vulnerabilities": frozenset({"vulnerabilities"}),
    "versions": frozenset({"versions", "changes", "impact"}),
    "changes": frozenset({"versions", "changes", "impact"}),
}


def scope_for_scan(conn: sqlite3.Connection, scan_id: str) -> dict[str, bool]:
    request = scan_store.request_for_scan(conn, scan_id)
    return scope_of(request["scope"] if request else None)


class Stages:
    """Writes stage events for one scan request. With no request id every call is a no-op."""

    def __init__(self, conn: sqlite3.Connection, request_id: str | None, only: frozenset[str] | None = None):
        self.conn = conn
        self.request_id = request_id
        self.only = only  # a retry touches only these stages; the others keep their last status

    def __call__(self, stage: str, status: str, summary: str | None = None, data: dict | None = None) -> None:
        if self.request_id and (self.only is None or stage in self.only):
            scan_store.add_scan_event(self.conn, self.request_id, stage, status, summary, data)

    def cancelled(self) -> bool:
        if not self.request_id:
            return False
        request = scan_store.get_scan_request(self.conn, self.request_id)
        return bool(request and request["status"] == "cancelled")

    async def guard(self, stage: str, work: Callable[[], Awaitable[str | None]]) -> bool:
        """Run one stage: `running`, then `done` with the summary `work` returns, or `error`. True when it worked."""
        if self.only is not None and stage not in self.only:
            return True
        self(stage, "running")
        try:
            summary = await work()
        except Exception as exc:
            logger.exception("scan stage %s failed", stage, extra={"source": stage})
            self(stage, "error", f"{type(exc).__name__}: {exc}"[:300])
            if self.request_id:
                scan_store.update_scan_request(self.conn, self.request_id, error=f"{stage}: {exc}"[:300])
            return False
        self(stage, "done", summary)
        return True


async def run_layers(
    conn: sqlite3.Connection, http: HttpClient | None, settings: Settings, scan_id: str, scope: dict[str, bool],
    stages: Stages, *, vulnerabilities_enabled: bool = True,
) -> None:
    """SBOM, internal dependencies, symbol index and vulnerabilities for a scan that was already ingested."""
    from app.orchestrator.store import scan_repo

    clone = scan_repo(conn, scan_id)["clone_path"]
    components_ready = False

    async def sbom_stage() -> str:
        nonlocal components_ready
        components, bom = sbom_module.build_sbom(clone or ".")
        scan_store.replace_components(conn, scan_id, components)
        components_ready = True
        if scope["sbom"]:
            sbom_module.write_sbom(bom, Path(settings.sbom_dir), scan_id)
        counts = scan_store.component_counts(conn, scan_id)
        if not components:
            warnings = [p["value"] for p in bom["metadata"].get("properties", []) if p["name"] == "pilot:warning"]
            return "No lockfile, so no bill of materials" + (f" ({warnings[0]})" if warnings else "")
        return f"{counts['total']} packages from the lockfile, {counts['direct']} declared directly"

    if scope["sbom"] or scope["vulnerabilities"]:
        await stages.guard("sbom", sbom_stage)
    else:
        stages("sbom", "skipped", "Not selected for this scan")
    if stages.cancelled():
        return

    stages("internal", "skipped",
           "Not selected for this scan" if not scope["internal_contracts"]
           else "Internal specs, API clients and topics are not read in this version, so none were looked for")
    stages("layer2", "skipped", "The symbol index (scip-typescript) is not enabled in this version")

    if scope["vulnerabilities"] and not vulnerabilities_enabled:
        stages("vulnerabilities", "skipped", "The advisory lookup is switched off for this run")
    elif scope["vulnerabilities"]:
        async def vuln_stage() -> str:
            if http is None:
                raise RuntimeError("no HTTP client available for the advisory lookup")
            await vulns_module.scan_vulnerabilities(
                conn, http, scan_id, scan_store.load_components(conn, scan_id) if components_ready else None)
            extra = await _dependabot(conn, http, settings, scan_id)
            rows = scan_store.list_scan_vulns(conn, scan_id)
            applies = sum(r["verdict"] == "applies" for r in rows)
            total = scan_store.component_counts(conn, scan_id)["total"]
            if not rows:
                return f"No known advisories in {total} packages{extra}"
            return f"{len(rows)} advisories in {total} packages, {applies} apply to your code{extra}"

        await stages.guard("vulnerabilities", vuln_stage)
    else:
        stages("vulnerabilities", "skipped", "Not selected for this scan")


async def _dependabot(conn: sqlite3.Connection, http: HttpClient, settings: Settings, scan_id: str) -> str:
    """Add Dependabot's open alerts when a token can read them. Returns a sentence fragment for the stage summary."""
    import re

    from app.changes.sources import dependabot
    from app.core.workspace import repo_identity
    from app.updates.github import GitHubClient

    repo_url = run_store.scan_repo(conn, scan_id)["repo_url"]
    slug = repo_identity(repo_url)
    if not settings.github_token or not (slug and re.fullmatch(r"[\w.-]+/[\w.-]+", slug)):
        return ""
    result = await dependabot.sync_scan(conn, GitHubClient(http), slug, repo_url or slug, scan_id)
    if result["status"] == "needs_permission":
        return " (Dependabot alerts could not be read: see Channels)"
    return f"; Dependabot reports {result['alerts']} open alert{'s' if result['alerts'] != 1 else ''}"


def finish_if_done(conn: sqlite3.Connection, scan_id: str) -> bool:
    """Close the scan request once no upgrade run of the scan is still working. True when it was closed."""
    request = scan_store.request_for_scan(conn, scan_id)
    if request is None or request["status"] in ("complete", "failed", "cancelled"):
        return False
    counts = run_store.status_counts_for_scan(conn, scan_id)
    if any(counts.get(s, 0) for s in ACTIVE_RUN_STATUSES):
        return False
    stages = Stages(conn, request["id"])
    total = sum(counts.values())
    if total == 0:
        stages("impact", "skipped", "No upgrade needs attention")
    else:
        waiting = counts.get("awaiting_approval", 0)
        noun = "upgrade" if total == 1 else "upgrades"
        stages("impact", "done", f"{total} {noun} analysed, {waiting} waiting for a decision")
    scan_store.update_scan_request(conn, request["id"], status="complete")
    return True
