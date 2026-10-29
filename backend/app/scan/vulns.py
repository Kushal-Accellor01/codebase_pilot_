"""S1: every package of the SBOM against OSV, and whether each advisory applies to the code (Part 3.3).

The verdict comes from code, never from the model:

    applies          runtime package, and a dependency path starts at a package our code imports
    does_not_apply   runtime package that no imported package leads to
    not_shipped      reachable only through dev dependencies (tests, tooling)
    undetermined     anything we cannot decide: nothing imported was found, or the advisory names an affected
                     function (that needs symbol analysis, Layer 2)

Without Layer 2, `applies` means "the vulnerable package is on a path our code loads".
"""
import json
import logging
import sqlite3

from app.scan import store as scan_store
from app.scan.sbom import Component
from app.updates import store as updates_store
from app.updates.http import HttpClient
from app.updates.security import check_security

logger = logging.getLogger(__name__)


def imported_packages(conn: sqlite3.Connection, scan_id: str) -> set[str]:
    """Packages that a non-test source file imports (Layer 1)."""
    from app.remediation.edits import is_test_file

    return {name for name, path in scan_store.importing_package_names(conn, scan_id) if not is_test_file(path)}


def names_function(raw: dict | None, package: str) -> bool:
    """Does the advisory say which function is affected? Then only a reference check can decide (Layer 2)."""
    for affected in (raw or {}).get("affected") or []:
        if (affected.get("package") or {}).get("name") != package:
            continue
        specific = affected.get("ecosystem_specific") or {}
        if any(specific.get(key) for key in ("functions", "affected_functions", "imports")):
            return True
    return False


def verdict(component: Component, imported: set[str], raw: dict | None = None) -> tuple[str, str]:
    """(verdict, one-sentence reason) by the rules above."""
    chain = " › ".join(component.path)
    if component.scope == "dev":
        return "not_shipped", f"Reached only through development tooling ({chain}); production builds leave it out."
    if not imported:
        return "undetermined", "No imports from your code were found, so Pilot cannot say whether it is loaded."
    entry = component.path[0] if component.path else component.name
    if entry not in imported:
        return "does_not_apply", f"No package your code imports leads to it ({chain})."
    if names_function(raw, component.name):
        return "undetermined", (
            f"{component.name} is loaded through {entry}, but the advisory names an affected function and Pilot "
            "has no symbol index to check whether your code reaches it.")
    if component.direct:
        return "applies", f"Your code imports {component.name} directly."
    return "applies", f"Your code imports {entry}, which loads {component.name} ({chain})."


async def scan_vulnerabilities(
    conn: sqlite3.Connection, http: HttpClient, scan_id: str, components: list[Component] | None = None,
) -> list[dict]:
    """Check every (name, version) of the scan and store one row per advisory and package, with its verdict."""
    components = components if components is not None else scan_store.load_components(conn, scan_id)
    if not components:
        scan_store.replace_scan_vulns(conn, scan_id, [])
        return []
    hits = await check_security(http, conn, sorted({(c.name, c.version) for c in components}))
    imported = imported_packages(conn, scan_id)
    best: dict[tuple[str, str, str], dict] = {}
    for component in components:
        for vuln in hits.get((component.name, component.version), []):
            record = updates_store.load_vulnerability(conn, vuln.id) or {}
            aliases = record.get("aliases") or []
            aliases = json.loads(aliases) if isinstance(aliases, str) else aliases
            decided, reason = verdict(component, imported, record.get("raw"))
            row = {
                "vuln_id": vuln.id, "name": component.name, "version": component.version, "purl": component.purl,
                "scope": component.scope, "direct": component.direct, "path": component.path,
                "severity": vuln.severity, "summary": vuln.summary, "aliases": aliases,
                "fixed_in": vuln.fixed_in, "verdict": decided, "reason": reason, "sources": ["osv"], "url": vuln.url,
            }
            key = (vuln.id, component.name, component.version)
            held = best.get(key)
            rank = (scan_store.VERDICT_RANK[decided], len(component.path))
            if held is None or rank < (scan_store.VERDICT_RANK[held["verdict"]], len(held["path"])):
                best[key] = row  # the worst case across copies of the package wins, then the shortest chain
    rows = list(best.values())
    scan_store.replace_scan_vulns(conn, scan_id, rows)
    return rows
