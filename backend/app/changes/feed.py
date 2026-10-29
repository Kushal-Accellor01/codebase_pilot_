"""S7: put what a scan found on the update feed, as `ChangeEvent`s of the common shape.

Three kinds of event come out of one scan (Dependabot alerts are recorded by their own source):
  registry   a dependency has a newer version that needs attention, with the quoted breaking changes
  osv        an advisory that applies to the code (and that Dependabot did not also report)
  push       an upgrade Pilot made was merged: the loop closed

Ids are stable (`make_event_id`), so a re-scan updates events instead of adding them.
"""
import sqlite3
from datetime import datetime, timezone

from app.changes import store as change_store
from app.core.contracts import ChangeEvent, ChangeItem, ChangeSubject, Severity
from app.orchestrator import store as run_store
from app.scan import store as scan_store
from app.updates import store as updates_store
from app.updates.events import build_event, wants_event

ITEM_KIND = {"removed": "removal", "signature_changed": "breaking", "behavior_changed": "behavior",
             "deprecated": "deprecation", "added": "addition", "security": "security"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _severity(value: str | None) -> Severity:
    return value if value in ("critical", "high", "medium", "low") else "none"  # type: ignore[return-value]


def registry_event(conn: sqlite3.Connection, repo: str, scan_id: str, row, importers: dict) -> ChangeEvent:
    dep = build_event(conn, row, importers)
    items = [
        ChangeItem(kind=ITEM_KIND.get(c.kind, "breaking" if c.breaking else "behavior"),
                   symbol=(c.api_symbols or [None])[0], summary=c.description, quote=c.evidence_quote or c.description,
                   evidence_url=c.evidence_url, verified=bool(c.evidence_quote))  # quotes were checked by Step 2
        for c in dep.changes
    ]
    return ChangeEvent(
        event_id=change_store.make_event_id("registry", repo, "package", dep.package, dep.current_version,
                                            dep.target_version),
        origin="external", source="registry", repo=repo, scan_id=scan_id,
        subject=ChangeSubject(kind="package", ecosystem=dep.ecosystem, name=dep.package, provider=dep.source_repo),
        from_version=dep.current_version, to_version=dep.target_version, detected_at=_now(), items=items,
        vulnerabilities=dep.vulnerabilities, source_ref=next((i.evidence_url for i in items if i.evidence_url), None),
        severity_hint=_severity(row["risk_level"]),
    )


def advisory_event(repo: str, scan_id: str, vuln: dict) -> ChangeEvent:
    from app.core.contracts import VulnRef

    summary = vuln["summary"] or vuln["vuln_id"]
    return ChangeEvent(
        event_id=change_store.make_event_id("osv", repo, "package", vuln["name"], vuln["version"], vuln["vuln_id"]),
        origin="external", source="osv", repo=repo, scan_id=scan_id,
        subject=ChangeSubject(kind="package", ecosystem="npm", name=vuln["name"]),
        from_version=vuln["version"], to_version=vuln["fixed_in"], detected_at=_now(),
        items=[ChangeItem(kind="security", summary=summary, quote=summary, evidence_url=vuln["url"], verified=True)],
        vulnerabilities=[VulnRef(id=vuln["vuln_id"], severity=vuln["severity"], summary=summary,
                                 fixed_in=vuln["fixed_in"], url=vuln["url"])],
        source_ref=vuln["url"], severity_hint=_severity(vuln["severity"]),
    )


def closure_event(repo: str, scan_id: str | None, run) -> ChangeEvent:
    return ChangeEvent(
        event_id=change_store.make_event_id("push", repo, "package", run["package"], run["from_version"],
                                            run["to_version"]),
        origin="internal", source="push", repo=repo, scan_id=scan_id,
        subject=ChangeSubject(kind="package", ecosystem=run["ecosystem"], name=run["package"]),
        from_version=run["from_version"], to_version=run["to_version"], detected_at=_now(), items=[],
        source_ref=None, severity_hint="none",
    )


def record_scan_changes(conn: sqlite3.Connection, scan_id: str) -> int:
    """Create or refresh the events of one scan and link them to their runs. Returns how many are new."""
    repo = run_store.scan_repo(conn, scan_id)["repo_url"] or scan_id
    importers = updates_store.importing_files_by_dep(conn, scan_id)
    runs = run_store.runs_for_scan(conn, scan_id)
    new = 0
    for row in updates_store.list_dependency_updates(conn, scan_id):
        if not wants_event(row) or not row["current_version"] or not row["recommended_target"]:
            continue
        run = runs.get(row["id"])
        new += change_store.upsert_event(conn, registry_event(conn, repo, scan_id, row, importers),
                                         run_id=run["id"] if run else None)
    for vuln in scan_store.list_scan_vulns(conn, scan_id):
        if vuln["verdict"] == "applies" and vuln["sources"] == ["osv"]:
            event = advisory_event(repo, scan_id, vuln)
            run = next((r for r in runs.values() if r["package"] == vuln["name"]), None)
            new += change_store.upsert_event(conn, event, run_id=run["id"] if run else None)
    return new


def record_closures(conn: sqlite3.Connection, run_ids: list[str]) -> int:
    new = 0
    for run_id in run_ids:
        run = run_store.get_run(conn, run_id)
        repo = run_store.scan_repo(conn, run["scan_id"])["repo_url"] or run["scan_id"]
        earlier = change_store.make_event_id("registry", repo, "package", run["package"], run["from_version"],
                                             run["to_version"])
        if change_store.get_event(conn, earlier) is not None:  # the card already on the feed closes
            change_store.set_status(conn, earlier, "resolved")
            continue
        event = closure_event(repo, run["scan_id"], run)
        new += change_store.upsert_event(conn, event, run_id=run_id)
        change_store.set_status(conn, event.event_id, "resolved")
    return new
