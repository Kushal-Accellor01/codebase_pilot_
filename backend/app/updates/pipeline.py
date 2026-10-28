"""Step 2 pipeline (deterministic part): inventory -> registry -> versions -> priority -> risk -> upsert.

Notes, source repositories, OSV and LLM analysis are added by later tickets. Until then a package that
needs attention is stored with status 'pending' and a provisional risk level.
"""
import asyncio
import logging
import time
import uuid
from collections import Counter

from app.updates import store
from app.updates.http import HttpClient
from app.updates.models import Dependency, RunStats, UpdateRow, UpdateVerdict
from app.updates.preflight import run_preflight
from app.updates.registry.base import RegistryAdapter
from app.updates.registry.batch import BatchResult, fetch_many
from app.updates.registry.npm import NpmRegistry
from app.updates.risk import RiskFacts, needs_attention, priority, recommended_target, risk_level
from app.updates.versions import parse_scan_time, resolve_versions

logger = logging.getLogger(__name__)

SUPPORTED_ECOSYSTEMS = {"npm"}


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


def evaluate(scan_id: str, dep: Dependency, batch: BatchResult, scan_time) -> UpdateRow:
    """Turn one dependency plus registry data into a row. Pure: no I/O."""
    row = _base_row(scan_id, dep)

    if dep.ecosystem not in SUPPORTED_ECOSYSTEMS:
        row.status = "unsupported_ecosystem"
        return row
    if dep.name in batch.errors:
        row.status, row.error = "error", batch.errors[dep.name]
        return row
    info = batch.packages.get(dep.name)
    if info is None:  # not fetched (should not happen); never silently drop a dependency
        row.status, row.error = "error", "registry data missing"
        return row
    if info.status == "not_found":
        row.status = "not_in_registry"
        return row

    verdict: UpdateVerdict = resolve_versions(dep.declared_constraint, info, scan_time)
    if verdict.error:
        row.status = "invalid_declaration" if verdict.error_kind == "invalid_declaration" else "error"
        row.error = verdict.error
        row.current_version_basis = verdict.current_version_basis
        row.latest_version = verdict.latest_version
        return row

    facts = RiskFacts.from_verdict(verdict, dep.dependency_type, dep.importer_count)
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
    row.recommended_target = recommended_target(verdict)
    row.priority_score = priority(facts)
    row.risk_level = risk_level(facts)
    row.status = "pending" if needs_attention(facts) else "up_to_date"
    return row


def _stats(rows: list[UpdateRow], undeclared: int, duration: float) -> RunStats:
    stats = RunStats(deps_total=len(rows), undeclared_imports=undeclared, duration_s=round(duration, 2))
    stats.statuses = dict(Counter(r.status for r in rows))
    stats.up_to_date = stats.statuses.get("up_to_date", 0)
    stats.errors = stats.statuses.get("error", 0)
    stats.notes_unavailable = stats.statuses.get("notes_unavailable", 0)
    stats.source_unknown = stats.statuses.get("source_unknown", 0)
    stats.analysed = stats.statuses.get("analyzed", 0)
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
) -> tuple[str, RunStats]:
    """Run the step for one scan. Returns (run_id, stats).

    One package failing never stops the run: it becomes a row with status 'error' and the run is
    marked 'partial'. Re-running updates the same rows.
    """
    started = time.perf_counter()
    conn = store.connect(db_path)
    run_id = str(uuid.uuid4())
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
        try:
            rows = await _process(conn, scan_id, scan_time, deps, refresh, registry)
            stats = _stats(rows, store.count_undeclared_imports(conn, scan_id), time.perf_counter() - started)
            status = "partial" if stats.errors else "succeeded"
            store.finish_pipeline_run(conn, run_id, status, stats.model_dump())
        except Exception as exc:
            store.finish_pipeline_run(conn, run_id, "failed", None, f"{type(exc).__name__}: {exc}")
            raise
        return run_id, stats
    finally:
        conn.close()


async def _process(conn, scan_id, scan_time, deps, refresh, registry) -> list[UpdateRow]:
    npm_names = [d.name for d in deps if d.ecosystem == "npm"]
    batch = BatchResult()
    if npm_names:
        if registry is not None:
            batch = await fetch_many(registry, conn, npm_names, refresh=refresh)
        else:
            async with HttpClient() as http:
                batch = await fetch_many(NpmRegistry(http), conn, npm_names, refresh=refresh)

    rows: list[UpdateRow] = []
    for dep in deps:
        try:
            row = evaluate(scan_id, dep, batch, scan_time)
        except Exception as exc:  # one package never stops the run
            logger.exception("evaluation failed for %s", dep.name, extra={"package": dep.name})
            row = _base_row(scan_id, dep)
            row.status, row.error = "error", f"{type(exc).__name__}: {exc}"
        try:
            store.upsert_dependency_update(conn, row)
        except Exception as exc:
            logger.exception("saving failed for %s", dep.name, extra={"package": dep.name})
            row.status, row.error = "error", f"could not save: {exc}"
        rows.append(row)
    return rows


def run_dependency_updates(db_path: str, scan: str = "latest", **kwargs) -> tuple[str, RunStats]:
    """Synchronous entry point (used by ingestion's --with-updates hook and the worker)."""
    return asyncio.run(run_dependency_updates_async(db_path, scan, **kwargs))
