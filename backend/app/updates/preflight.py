import sqlite3

from app.updates import store
from app.updates.models import PreflightReport


class PreflightError(RuntimeError):
    pass


def resolve_scan(conn: sqlite3.Connection, scan: str = "latest") -> str:
    if scan == "latest":
        scan_id = store.latest_scan_id(conn)
        if scan_id is None:
            raise PreflightError("No scan with nodes found. Run ingestion first.")
        return scan_id
    if not store.scan_exists(conn, scan):
        raise PreflightError(f"Scan not found: {scan}")
    return scan


def run_preflight(conn: sqlite3.Connection, scan: str = "latest") -> PreflightReport:
    """Fails (raises) only on: no scan, no dependencies. Everything else is a warning."""
    scan_id = resolve_scan(conn, scan)
    deps = store.count_dependencies(conn, scan_id)
    if deps == 0:
        raise PreflightError(f"Scan {scan_id} has no dependency nodes.")

    no_constraint = store.count_declares_without_constraint(conn, scan_id)
    no_type = store.count_declares_without_type(conn, scan_id)
    lockfiles = store.count_lockfiles(conn, scan_id)

    warnings: list[str] = []
    if no_constraint:
        warnings.append(f"{no_constraint} DECLARES edges have no declared_constraint")
    if no_type:
        warnings.append(f"{no_type} DECLARES edges have no dependency_type (treated as 'unknown')")
    return PreflightReport(
        scan_id=scan_id,
        dependencies=deps,
        declared_without_constraint=no_constraint,
        missing_dependency_type=no_type,
        lockfiles=lockfiles,
        warnings=warnings,
    )
