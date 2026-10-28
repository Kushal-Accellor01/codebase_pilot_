"""Step 2 pipeline (S2 section 5.1-5.12), one run per scan:

  inventory -> registry -> versions -> OSV -> priority -> [source -> notes -> LLM analysis -> evidence] -> risk
  -> persist -> events/report

Every stage is optional so the deterministic core can run offline and tests can exercise one stage at a time.
One package failing never stops the run; non-fatal problems become warnings and mark the run 'partial'.
"""
import asyncio
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core.llm import LLM, LLMError, LLMNotConfigured
from app.updates import store
from app.updates.analysis import analyse_update
from app.updates.github import GitHubClient, GitHubRateLimited
from app.updates.http import HttpClient
from app.updates.lockfile import load_lockfile_versions
from app.updates.models import Dependency, RunStats, UpdateRow, UpdateVerdict
from app.updates.notes import fetch_notes
from app.updates.preflight import run_preflight
from app.updates.registry.base import PackageInfo, RegistryAdapter
from app.updates.registry.batch import BatchResult, fetch_many
from app.updates.registry.npm import NpmRegistry
from app.updates.risk import RiskFacts, needs_attention, priority, recommended_target, risk_level
from app.updates.security import PackageVuln, check_security, min_safe_version
from app.updates.sources import get_source, load_overrides
from app.updates.versions import compare_versions, parse_scan_time, resolve_versions, satisfies_range

logger = logging.getLogger(__name__)

SUPPORTED_ECOSYSTEMS = {"npm"}
SEVERITY_ORDER = ["unknown", "low", "medium", "high", "critical"]


# --- pure evaluation -----------------------------------------------------------------------------------------


@dataclass
class Evaluation:
    row: UpdateRow
    facts: RiskFacts | None = None
    verdict: UpdateVerdict | None = None
    info: PackageInfo | None = None


def _base_row(scan_id: str, dep: Dependency) -> UpdateRow:
    return UpdateRow(
        scan_id=scan_id,
        dep_node_id=dep.dep_node_id,
        ecosystem=dep.ecosystem,
        name=dep.name,
        manifest_file_id=dep.manifest_file_id,
        dependency_type=dep.dependency_type,
        declared_constraint=dep.declared_constraint,
        importer_count=dep.importer_count,
    )


def locked_version(dep: Dependency, lockfile: dict[str, str] | None) -> str | None:
    """The lockfile only describes the root manifest."""
    return (lockfile or {}).get(dep.name) if dep.manifest_path in ("package.json", "") else None


def _verdict(dep: Dependency, batch: BatchResult, scan_time, lockfile: dict[str, str] | None = None):
    info = batch.packages.get(dep.name)
    if info is None or info.status != "ok":
        return None
    return resolve_versions(dep.declared_constraint, info, scan_time, lockfile_version=locked_version(dep, lockfile))


def rescore(row: UpdateRow, facts: RiskFacts) -> None:
    row.priority_score = priority(facts)
    row.risk_level = risk_level(facts)


def evaluate(
    scan_id: str, dep: Dependency, batch: BatchResult, scan_time, vulns: list[PackageVuln] | None = None,
    lockfile: dict[str, str] | None = None,
) -> Evaluation:
    """Dependency + registry (+ OSV) data -> a row. Pure: no I/O."""
    row = _base_row(scan_id, dep)

    if dep.ecosystem not in SUPPORTED_ECOSYSTEMS:
        row.status = "unsupported_ecosystem"
        return Evaluation(row)
    if dep.name in batch.errors:
        row.status, row.error = "error", batch.errors[dep.name]
        return Evaluation(row)
    info = batch.packages.get(dep.name)
    if info is None:  # not fetched (should not happen); never silently drop a dependency
        row.status, row.error = "error", "registry data missing"
        return Evaluation(row)
    if info.status == "not_found":
        row.status = "not_in_registry"
        return Evaluation(row)

    verdict = resolve_versions(
        dep.declared_constraint, info, scan_time, lockfile_version=locked_version(dep, lockfile)
    )
    if verdict.error:
        row.status = "invalid_declaration" if verdict.error_kind == "invalid_declaration" else "error"
        row.error = verdict.error
        row.current_version_basis = verdict.current_version_basis
        row.latest_version = verdict.latest_version
        return Evaluation(row)

    vulns = vulns or []
    facts = RiskFacts.from_verdict(verdict, dep.dependency_type, dep.importer_count)
    facts.vuln_severities = [v.severity for v in vulns]
    safe = min_safe_version(vulns)

    row.current_version = verdict.current_version
    row.current_version_basis = verdict.current_version_basis
    row.latest_in_range = verdict.latest_in_range
    row.latest_version = verdict.latest_version
    row.update_type = verdict.update_type
    row.requires_manifest_change = verdict.requires_manifest_change
    row.versions_behind = verdict.versions_behind
    row.runtime_requirement_change = verdict.runtime_requirement_change
    row.is_deprecated = verdict.is_deprecated
    row.deprecation_message = verdict.deprecation_message
    row.vuln_count = len(vulns)
    row.max_severity = max((v.severity for v in vulns), key=SEVERITY_ORDER.index, default=None)
    row.min_safe_version = safe
    safe_in_range = bool(safe) and satisfies_range(safe, dep.declared_constraint)
    row.recommended_target = recommended_target(verdict, safe, safe_in_range)
    if safe and not safe_in_range:
        row.requires_manifest_change = True  # the security fix is outside the declared range
    rescore(row, facts)
    row.status = "pending" if needs_attention(facts) else "up_to_date"
    return Evaluation(row, facts, verdict, info)


# --- enrichment: source, notes, LLM -------------------------------------------------------------------------------


@dataclass
class Context:
    conn: object
    http: HttpClient
    gh: GitHubClient | None
    llm: LLM | None
    overrides: dict
    warnings: list[str] = field(default_factory=list)
    github_blocked: bool = False
    llm_blocked: bool = False
    analysis_cache_hits: int = 0

    def warn_once(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


async def enrich(ctx: Context, ev: Evaluation) -> int | None:
    """Source -> notes -> analysis for one package that has an update to look at.

    Mutates ev.row / ev.facts and returns the analysis id when one was stored. Only deterministic results
    survive any failure here.
    """
    row, facts, info, verdict = ev.row, ev.facts, ev.info, ev.verdict
    if row.status != "pending" or not (facts and info and verdict and ctx.gh and row.current_version):
        return None
    current, target = row.current_version, row.recommended_target
    if not target or compare_versions(target, current) <= 0 or ctx.github_blocked:
        return None

    try:
        source = await get_source(ctx.gh, ctx.http, ctx.conn, info, target, overrides=ctx.overrides)
        facts.archived = source.archived
        if source.status != "resolved":
            row.status = "source_unknown"
            rescore(row, facts)
            return None
        notes = await fetch_notes(ctx.gh, ctx.conn, source, info, current, target)
    except GitHubRateLimited as exc:
        ctx.github_blocked = True
        ctx.warn_once(f"{exc}; remaining packages left 'pending'")
        return None
    except Exception as exc:
        logger.exception("source/notes failed for %s", row.name, extra={"package": row.name})
        row.status, row.error = "error", f"notes lookup failed: {type(exc).__name__}: {exc}"
        return None

    if not notes.available:
        row.status = "notes_unavailable"
        facts.notes_unavailable = True
        rescore(row, facts)
        return None
    if ctx.llm is None or ctx.llm_blocked:
        rescore(row, facts)
        return None

    try:
        result = await analyse_update(
            ctx.llm, ctx.conn, ecosystem=row.ecosystem, name=row.name, current=current, target=target,
            update_type=verdict.update_type, engines_current=verdict.engines_current,
            engines_target=verdict.engines_target, deprecation_message=verdict.deprecation_message, notes=notes,
        )
    except LLMNotConfigured as exc:
        ctx.llm_blocked = True
        ctx.warn_once(f"LLM analysis skipped: {exc}")
        rescore(row, facts)
        return None
    except LLMError as exc:  # content filter, bad output, rejected credentials
        row.status, row.error = "error", f"analysis_failed: {exc}"
        return None

    if result.from_cache:
        ctx.analysis_cache_hits += 1
    facts.has_breaking = any(c.breaking for c in result.changes)
    row.status = "analyzed"
    rescore(row, facts)
    return result.analysis_id


def _clone_path(conn, scan_id: str) -> str | None:
    """Where the scanned code lives (scan_meta, else scans.repository_path); None if it is gone."""
    from pathlib import Path

    from app.orchestrator.store import scan_repo

    try:
        path = scan_repo(conn, scan_id)["clone_path"]
    except KeyError:
        return None
    return path if path and Path(path).is_dir() else None


# --- run --------------------------------------------------------------------------------------------------------------


def _stats(rows: list[UpdateRow], undeclared: int, duration: float, ctx: Context) -> RunStats:
    stats = RunStats(deps_total=len(rows), undeclared_imports=undeclared, duration_s=round(duration, 2))
    stats.warnings = ctx.warnings
    stats.statuses = dict(Counter(r.status for r in rows))
    stats.up_to_date = stats.statuses.get("up_to_date", 0)
    stats.errors = stats.statuses.get("error", 0)
    stats.notes_unavailable = stats.statuses.get("notes_unavailable", 0)
    stats.source_unknown = stats.statuses.get("source_unknown", 0)
    stats.analysed = stats.statuses.get("analyzed", 0)
    stats.llm_calls = ctx.llm.calls if ctx.llm else 0
    stats.cache_hits = ctx.analysis_cache_hits + (ctx.llm.cache_hits if ctx.llm else 0)
    for r in rows:
        if r.update_type in stats.updates and r.status not in ("error", "up_to_date"):
            stats.updates[r.update_type] += 1
        stats.vulnerable += 1 if r.vuln_count else 0
        stats.deprecated += 1 if r.is_deprecated else 0
    return stats


async def run_dependency_updates_async(
    db_path: str,
    scan: str = "latest",
    *,
    only: list[str] | None = None,
    refresh: bool = False,
    registry: RegistryAdapter | None = None,
    http: HttpClient | None = None,
    github: GitHubClient | None = None,
    llm: LLM | None = None,
    security: bool = True,
    notes: bool = True,
    analysis: bool = True,
    write_report: bool = True,
    reports_dir: str | Path | None = None,
) -> tuple[str, RunStats]:
    """Run the step for one scan. Returns (run_id, stats).

    security / notes / analysis switch the OSV, source+notes and LLM stages. Without `notes` there is no
    GitHub traffic; without `analysis` notes are fetched but not interpreted.
    """
    started = time.perf_counter()
    conn = store.connect(db_path)
    run_id = str(uuid.uuid4())
    own_http = http is None
    http = http or HttpClient()
    try:
        store.migrate(conn)
        report = run_preflight(conn, scan)  # raises if there is no scan or no dependencies
        scan_id = report.scan_id
        scan_time = parse_scan_time(store.scan_scanned_at(conn, scan_id))
        deps = store.load_inventory(conn, scan_id)
        if only:
            wanted = set(only)
            deps = [d for d in deps if d.name in wanted]
        store.start_pipeline_run(conn, run_id, scan_id)
        ctx = Context(
            conn=conn, http=http,
            gh=(github or GitHubClient(http)) if notes else None,
            llm=(llm or LLM(conn, http)) if (notes and analysis) else None,
            overrides=load_overrides() if notes else {},
        )
        try:
            lockfile = load_lockfile_versions(_clone_path(conn, scan_id))
            rows = await _process(
                conn, ctx, scan_id, scan_time, deps, refresh, registry or NpmRegistry(http), security, lockfile
            )
            stats = _stats(rows, store.count_undeclared_imports(conn, scan_id), time.perf_counter() - started, ctx)
            status = "partial" if stats.errors or ctx.warnings else "succeeded"
            store.finish_pipeline_run(conn, run_id, status, stats.model_dump())
            if write_report:
                from app.updates.report import write_reports

                write_reports(conn, scan_id, reports_dir)
        except Exception as exc:
            store.finish_pipeline_run(conn, run_id, "failed", None, f"{type(exc).__name__}: {exc}")
            raise
        return run_id, stats
    finally:
        conn.close()
        if own_http:
            await http.aclose()


async def _process(
    conn, ctx, scan_id, scan_time, deps, refresh, registry, security, lockfile=None
) -> list[UpdateRow]:
    npm_names = [d.name for d in deps if d.ecosystem == "npm"]
    batch = BatchResult()
    if npm_names:
        batch = await fetch_many(registry, conn, npm_names, refresh=refresh)

    vulns_by_target: dict[tuple[str, str], list[PackageVuln]] = {}
    if security:
        targets = []
        for dep in deps:
            if dep.ecosystem == "npm" and dep.name not in batch.errors:
                verdict = _verdict(dep, batch, scan_time, lockfile)
                if verdict and verdict.current_version:
                    targets.append((dep.name, verdict.current_version))
        if targets:
            try:
                vulns_by_target = await check_security(ctx.http, conn, targets)
            except Exception as exc:  # OSV down must not stop the run, but must not look clean either
                logger.exception("OSV check failed")
                ctx.warnings.append(f"security check failed: {type(exc).__name__}: {exc}")

    rows: list[UpdateRow] = []
    for dep in deps:
        vulns: list[PackageVuln] = []
        analysis_id: int | None = None
        try:
            verdict = _verdict(dep, batch, scan_time, lockfile) if dep.ecosystem == "npm" else None
            if verdict and verdict.current_version:
                vulns = vulns_by_target.get((dep.name, verdict.current_version), [])
            ev = evaluate(scan_id, dep, batch, scan_time, vulns, lockfile)
            analysis_id = await enrich(ctx, ev)
            row = ev.row
        except Exception as exc:  # one package never stops the run
            logger.exception("evaluation failed for %s", dep.name, extra={"package": dep.name})
            row = _base_row(scan_id, dep)
            row.status, row.error = "error", f"{type(exc).__name__}: {exc}"
        try:
            update_id = store.upsert_dependency_update(conn, row)
            store.replace_update_vulns(conn, update_id, [(v.id, v.fixed_in) for v in vulns])
            if analysis_id is not None:
                store.set_analysis(conn, update_id, analysis_id)
        except Exception as exc:
            logger.exception("saving failed for %s", dep.name, extra={"package": dep.name})
            row.status, row.error = "error", f"could not save: {exc}"
        rows.append(row)
    return rows


def run_dependency_updates(db_path: str, scan: str = "latest", **kwargs) -> tuple[str, RunStats]:
    """Synchronous entry point (used by ingestion's --with-updates hook and the worker)."""
    return asyncio.run(run_dependency_updates_async(db_path, scan, **kwargs))
