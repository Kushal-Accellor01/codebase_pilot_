"""Build DependencyChangeEvents (the contract to impact analysis, S2 section 9) from stored rows."""
import json
import sqlite3

from app.updates import store
from app.updates.models import ChangeRef, DependencyChangeEvent, VulnRef

RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
NEEDS_HELP = {"source_unknown", "notes_unavailable"}


def wants_event(row: sqlite3.Row) -> bool:
    """risk medium or higher; or a human has to help (no source / no notes) while an update exists."""
    if RISK_ORDER.get(row["risk_level"] or "none", 0) >= RISK_ORDER["medium"]:
        return True
    return row["status"] in NEEDS_HELP and (row["update_type"] or "none") != "none"


def build_event(
    conn: sqlite3.Connection, row: sqlite3.Row, importing_files: dict[str, list]
) -> DependencyChangeEvent:
    target = row["recommended_target"] or row["current_version"]
    summary, coverage, changes = None, None, []
    if row["analysis_id"]:
        analysis = store.get_analysis_row(conn, row["analysis_id"])
        if analysis is not None:
            summary, coverage = analysis["summary"], analysis["notes_coverage"]
            changes = [
                ChangeRef(
                    kind=c["kind"], breaking=bool(c["breaking"]), api_symbols=json.loads(c["api_symbols"]),
                    description=c["description"], migration_hint=c["migration_hint"], version=c["version"],
                    evidence_url=c["evidence_url"], evidence_quote=c["evidence_quote"],
                )
                for c in store.load_changes(conn, row["analysis_id"])
            ]
    source = store.load_package_source(conn, row["ecosystem"], row["name"])
    source_repo = source[0].slug if source and source[0].status == "resolved" else None
    manifest = row["manifest_file_id"] or ""
    return DependencyChangeEvent(
        event_id=f"{row['scan_id']}:{row['ecosystem']}:{row['name']}:{row['current_version']}->{target}",
        scan_id=row["scan_id"],
        ecosystem=row["ecosystem"],
        package=row["name"],
        dependency_type=row["dependency_type"],
        manifest_path=store.posix(manifest[5:] if manifest.startswith("file:") else manifest),
        declared_constraint=row["declared_constraint"],
        current_version=row["current_version"],
        current_version_basis=row["current_version_basis"] or "unresolvable",
        target_version=target,
        latest_version=row["latest_version"],
        update_type=row["update_type"] or "unknown",
        requires_manifest_change=bool(row["requires_manifest_change"]),
        runtime_requirement_change=bool(row["runtime_requirement_change"]),
        is_deprecated=bool(row["is_deprecated"]),
        deprecation_message=row["deprecation_message"],
        vulnerabilities=[
            VulnRef(id=v["id"], severity=v["severity"] or "unknown", summary=v["summary"], fixed_in=v["fixed_in"],
                    url=v["url"])
            for v in store.list_update_vulns(conn, row["id"])
        ],
        changes=changes,
        summary=summary,
        notes_coverage=coverage,
        source_repo=source_repo,
        importer_count=row["importer_count"],
        importing_files=importing_files.get(row["dep_node_id"], []),
        priority_score=row["priority_score"] or 0.0,
        risk_level=row["risk_level"] or "none",
        status=row["status"],
    )


def build_events(
    conn: sqlite3.Connection, scan_id: str, *, only_actionable: bool = True
) -> list[DependencyChangeEvent]:
    """Events for the scan's rows, highest priority first."""
    importers = store.importing_files_by_dep(conn, scan_id)
    rows = store.list_dependency_updates(conn, scan_id)
    return [build_event(conn, r, importers) for r in rows if not only_actionable or wants_event(r)]
