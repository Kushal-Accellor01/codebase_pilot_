"""All SQL the dashboard needs, and the sentence templates built on it (D1). Routes contain no SQL."""

import json
import sqlite3

from app.actions.codeowners import load_codeowners, owners_for
from app.impact import store as impact_store
from app.orchestrator import store as run_store
from app.updates import store as updates_store
from app.updates.events import RISK_ORDER, build_event, wants_event

RISKS = ("critical", "high", "medium", "low", "none")
TERMINAL = {"pr_opened", "rejected", "report_only", "needs_engineer", "failed", "resolved"}

# run status -> (label, pill class used by the dashboard)
RUN_LABELS = {
    "queued": ("Queued", "run"),
    "analyzing": ("Analyzing", "run"),
    "fixing": ("Writing the fix", "run"),
    "validating": ("Testing the fix", "run"),
    "awaiting_approval": ("Awaiting approval", "wait"),
    "approved": ("Approved", "run"),
    "rejected": ("Rejected", ""),
    "pr_opened": ("Pull request opened", "done"),
    "needs_engineer": ("Needs an engineer", "fail"),
    "report_only": ("Report only", ""),
    "failed": ("Failed", "fail"),
    "resolved": ("Resolved", "done"),
}


def plural(n: int, noun: str) -> str:
    if n == 1:
        return f"{n} {noun}"
    return f"{n} {noun[:-1]}ies" if noun.endswith("y") else f"{n} {noun}s"


class NotFound(LookupError):
    pass


def resolve_scan(conn: sqlite3.Connection, scan: str) -> str:
    scan_id = updates_store.latest_scan_id(conn) if scan == "latest" else scan
    if not scan_id or not updates_store.scan_exists(conn, scan_id):
        raise NotFound(f"scan {scan}")
    return scan_id


# --- scans ----------------------------------------


def list_scans(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for s in conn.execute("SELECT * FROM scans ORDER BY scanned_at DESC, rowid DESC"):
        sid = s["id"]
        has_nodes = conn.execute("SELECT 1 FROM nodes WHERE scan_id = ? LIMIT 1", (sid,)).fetchone() is not None
        info = updates_store.get_scan_info(conn, sid)
        meta = run_store.scan_repo(conn, sid)
        runs = _run_by_update(conn, sid)
        rows = updates_store.list_dependency_updates(conn, sid)
        updates = {
            "total": len(rows),
            "updates": sum(1 for r in rows if r["status"] not in ("up_to_date", "unsupported_ecosystem")),
            "critical": sum(1 for r in rows if effective_risk(r, runs.get(r["id"])) == "critical"),
        }
        out.append(
            {
                "id": sid,
                "repo": _repo_label(meta["repo_url"] or info["repository_path"]),
                "repo_url": meta["repo_url"],
                "commit_sha": info["commit_sha"],
                "scanned_at": info["scanned_at"],
                "has_nodes": has_nodes,
                "dependencies": conn.execute(
                    "SELECT count(*) FROM nodes WHERE scan_id = ? AND type = 'dependency'", (sid,)
                ).fetchone()[0]
                if has_nodes
                else 0,
                "analysed": updates["total"] or 0,
                "updates": updates["updates"] or 0,
                "critical": updates["critical"] or 0,
            }
        )
    return out


def effective_risk(update: sqlite3.Row, run: sqlite3.Row | None) -> str:
    """The run's risk (the plan's severity, which looks at the actual code) beats Step 2's provisional level."""
    if run is not None and run["risk_level"]:
        return run["risk_level"]
    return update["risk_level"] or "none"


def _repo_label(value: str | None) -> str:
    if not value:
        return "unknown"
    cleaned = value.replace("\\", "/").rstrip("/").removesuffix(".git")
    return cleaned.rsplit("/", 1)[-1] or cleaned


# --- dependencies ----------------------------------------


def _run_by_update(conn: sqlite3.Connection, scan_id: str) -> dict[int, sqlite3.Row]:
    return {
        r["dependency_update_id"]: r for r in conn.execute("SELECT * FROM upgrade_runs WHERE scan_id = ?", (scan_id,))
    }


def _reachable_updates(conn: sqlite3.Connection, scan_id: str) -> set[int]:
    rows = conn.execute(
        """SELECT DISTINCT u.id FROM dependency_updates u
           JOIN upgrade_runs r ON r.dependency_update_id = u.id
           JOIN impact_findings f ON f.upgrade_run_id = r.id
           WHERE u.scan_id = ? AND (f.confirmed IS NULL OR f.confirmed = 1)""",
        (scan_id,),
    ).fetchall()
    return {r["id"] for r in rows}


def reachability(row: sqlite3.Row, reachable: set[int]) -> str:
    """reachable_in_code: confirmed findings exist; imported: the code imports it; declared_only: only the manifest."""
    if row["id"] in reachable:
        return "reachable_in_code"
    return "imported" if row["importer_count"] > 0 else "declared_only"


def list_dependencies(conn: sqlite3.Connection, scan_id: str) -> list[dict]:
    runs = _run_by_update(conn, scan_id)
    reachable = _reachable_updates(conn, scan_id)
    out = []
    for r in updates_store.list_dependency_updates(conn, scan_id):
        run = runs.get(r["id"])
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "ecosystem": r["ecosystem"],
                "dependency_type": r["dependency_type"],
                "declared_constraint": r["declared_constraint"],
                "current_version": r["current_version"],
                "latest_in_range": r["latest_in_range"],
                "latest_version": r["latest_version"],
                "recommended_target": r["recommended_target"],
                "update_type": r["update_type"],
                "requires_manifest_change": bool(r["requires_manifest_change"]),
                "vuln_count": r["vuln_count"],
                "max_severity": r["max_severity"],
                "is_deprecated": bool(r["is_deprecated"]),
                "importer_count": r["importer_count"],
                "risk_level": effective_risk(r, run),
                "priority_score": r["priority_score"] or 0.0,
                "status": r["status"],
                "error": r["error"],
                "reachability": reachability(r, reachable),
                "run_id": run["id"] if run else None,
                "run_status": run["status"] if run else None,
            }
        )
    return out


def get_dependency_event(conn: sqlite3.Connection, update_id: int) -> dict:
    row = updates_store.get_update_row(conn, update_id)
    if row is None:
        raise NotFound(f"dependency update {update_id}")
    importers = updates_store.importing_files_by_dep(conn, row["scan_id"])
    event = build_event(conn, row, importers).model_dump(mode="json")
    run = _run_by_update(conn, row["scan_id"]).get(update_id)
    event["id"] = update_id
    event["run_id"] = run["id"] if run else None
    event["run_status"] = run["status"] if run else None
    event["error"] = row["error"]
    return event


# --- overview ----------------------------------------


def _run_facts(conn: sqlite3.Connection, run_id: str) -> dict:
    findings = [f for f in run_store.load_findings(conn, run_id) if f.confirmed is not False]
    telemetry = run_store.load_telemetry(conn, run_id)
    symbols = {t.related_symbol for t in telemetry if t.related_symbol}
    warned = {f.line for f in findings if f.symbol in symbols}
    plan = run_store.load_plan(conn, run_id)
    patches = run_store.load_patches(conn, run_id)
    return {
        "lines": len(findings),
        "files": len({f.file_path for f in findings}),
        "in_production": len(warned),
        "plan": plan,
        "tests_pass": bool(patches and (patches[-1]["validation"] or {}).get("passed")),
        "effort": plan.effort_hours if plan else None,
    }


def why_sentence(row: sqlite3.Row, facts: dict | None, target: str | None) -> tuple[str, str]:
    """(sentence, bold part). Templates follow the dashboard mockup."""
    if facts and facts["lines"]:
        bold = f"{plural(facts['lines'], 'line')} in {plural(facts['files'], 'file')}"
        text = f"{bold} use APIs that change in {target}"
        if facts["in_production"]:
            n = facts["in_production"]
            text += f"; {n} already {'warns' if n == 1 else 'warn'} in production"
        return text, bold
    if row["vuln_count"]:
        bold = "Security fix"
        return f"{bold} for {row['vuln_count']} known {'advisory' if row['vuln_count'] == 1 else 'advisories'}", bold
    if row["runtime_requirement_change"]:
        return "Needs a newer Node.js version", "newer Node.js"
    if row["is_deprecated"]:
        return "The current version is deprecated", "deprecated"
    return f"A newer {row['update_type'] or ''} version is available".replace("  ", " "), ""


def headline(counts: dict, top: dict | None) -> tuple[str, str]:
    decisions = counts["awaiting"]
    if decisions and top:
        t = top
        sentence = f"{decisions} {'upgrade needs' if decisions == 1 else 'upgrades need'} a decision."
        if t["lines"]:
            tail = f" {t['package'].capitalize()} {t['to'].split('.')[0]} would break {plural(t['lines'], 'line')}"
            if t["in_production"]:
                verb = "warns" if t["in_production"] == 1 else "warn"
                tail += f", and {t['in_production']} of them already {verb} in production"
            sentence += tail + "."
        checked = plural(counts["total"], "dependency")
        lede = f"Pilot checked {checked} against their registries, release notes and your code."
        return sentence, lede + " Every finding links to its evidence."
    if counts["attention"]:
        return (
            f"{counts['attention']} {'dependency has' if counts['attention'] == 1 else 'dependencies have'} "
            "updates worth a look.",
            f"Pilot checked {plural(counts['total'], 'dependency')}. Nothing needs a decision yet.",
        )
    return "Everything is up to date.", f"Pilot checked {plural(counts['total'], 'dependency')}."


def overview(conn: sqlite3.Connection, scan_id: str) -> dict:
    rows = updates_store.list_dependency_updates(conn, scan_id)
    runs = _run_by_update(conn, scan_id)
    risk = {k: 0 for k in RISKS}
    board, waiting = [], []
    affected: dict[str, dict] = {}
    security = []
    top = None

    for r in rows:
        run = runs.get(r["id"])
        level = effective_risk(r, run)
        risk[level] = risk.get(level, 0) + 1
        if r["vuln_count"]:
            security.append(
                {
                    "package": r["name"],
                    "version": r["current_version"],
                    "count": r["vuln_count"],
                    "severity": r["max_severity"],
                    "fixed_in": r["min_safe_version"],
                }
            )
        if not run and not wants_event(r):
            continue
        facts = _run_facts(conn, run["id"]) if run else None
        target = r["recommended_target"]
        sentence, bold = why_sentence(r, facts, target)
        label, pill = RUN_LABELS.get(run["status"], (run["status"], "")) if run else ("No action", "done")
        item = {
            "id": r["id"],
            "run_id": run["id"] if run else None,
            "package": r["name"],
            "type": r["dependency_type"],
            "from": r["current_version"],
            "to": target,
            "kind": r["update_type"] or "none",
            "risk": level,
            "why": sentence,
            "why_bold": bold,
            "status": run["status"] if run else r["status"],
            "status_label": label,
            "pill": pill,
            "effort_hours": facts["effort"] if facts else None,
            "tests_pass": facts["tests_pass"] if facts else False,
        }
        board.append(item)
        if run and run["status"] == "awaiting_approval":
            detail = []
            if facts:
                detail.append("Fix tested" if facts["tests_pass"] else "Needs an engineer")
                detail.append(f"{facts['lines']} {'change' if facts['lines'] == 1 else 'changes'}")
            waiting.append({**item, "detail": " · ".join(detail)})
        if facts and top is None and run:
            top = {
                "package": r["name"],
                "to": target or "",
                "lines": facts["lines"],
                "in_production": facts["in_production"],
            }
        if run:
            for f in run_store.load_findings(conn, run["id"], include_rejected=False):
                a = affected.setdefault(
                    f.file_path, {"path": f.file_path, "lines": 0, "packages": set(), "evidence": set()}
                )
                a["lines"] += 1
                a["packages"].add(r["name"])
                a["evidence"].update(f.sources)

    board.sort(key=lambda b: (-RISK_ORDER.get(b["risk"], 0), b["effort_hours"] or 0, b["package"]))
    order = ["test_failure", "runtime_deprecation", "pattern"]
    labels = {"test_failure": "Failing test", "runtime_deprecation": "Runtime warning", "pattern": "Code search"}
    files = sorted(
        (
            {
                "path": a["path"],
                "lines": a["lines"],
                "packages": sorted(a["packages"]),
                "strongest_evidence": next((labels[s] for s in order if s in a["evidence"]), "Code search"),
            }
            for a in affected.values()
        ),
        key=lambda a: (-a["lines"], a["path"]),
    )[:8]
    counts = {
        "total": len(rows),
        "awaiting": len(waiting),
        "attention": sum(1 for r in rows if r["status"] not in ("up_to_date", "unsupported_ecosystem")),
    }
    text, lede = headline(counts, top)
    return {
        "scan_id": scan_id,
        "headline": text,
        "lede": lede,
        "risk": risk,
        "total": len(rows),
        "upgrades": board,
        "waiting": waiting,
        "security": security,
        "affected_files": files,
        "approvals_waiting": len(waiting),
    }


# --- upgrade runs ----------------------------------------


def _run_summary(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    label, pill = RUN_LABELS.get(run["status"], (run["status"], ""))
    plan = run_store.load_plan(conn, run["id"])
    return {
        "id": run["id"],
        "scan_id": run["scan_id"],
        "package": run["package"],
        "ecosystem": run["ecosystem"],
        "from": run["from_version"],
        "to": run["to_version"],
        "status": run["status"],
        "status_label": label,
        "pill": pill,
        "risk": run["risk_level"] or "none",
        "current_node": run["current_node"],
        "error": run["error"],
        "effort_hours": plan.effort_hours if plan else None,
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
    }


def list_upgrades(conn: sqlite3.Connection, status: str | None = None, scan_id: str | None = None) -> list[dict]:
    runs = run_store.list_runs(conn, status)
    if scan_id:
        runs = [r for r in runs if r["scan_id"] == scan_id]
    runs.sort(key=lambda r: (-RISK_ORDER.get(r["risk_level"] or "none", 0), r["created_at"]))
    return [_run_summary(conn, r) for r in runs]


def _probe_events(conn: sqlite3.Connection, run_id: str) -> dict:
    """Test results recorded by the impact step (current version and unpatched target)."""
    out: dict = {}
    for r in conn.execute(
        "SELECT node, data FROM run_events WHERE run_id = ? AND node IN ('baseline_probe','upgrade_probe') "
        "AND status = 'finished' ORDER BY id",
        (run_id,),
    ):
        out[r["node"]] = json.loads(r["data"]) if r["data"] else {}
    return out


def get_upgrade(conn: sqlite3.Connection, run_id: str) -> dict:
    run = run_store.get_run(conn, run_id)
    if run is None:
        raise NotFound(f"upgrade run {run_id}")
    update = updates_store.get_update_row(conn, run["dependency_update_id"])
    event = get_dependency_event(conn, update["id"])
    findings = run_store.load_findings(conn, run_id)
    approval = run_store.load_approval(conn, run_id)
    pr = run_store.load_pull_request(conn, run_id)
    patches = run_store.load_patches(conn, run_id)
    plan = run_store.load_plan(conn, run_id)
    facts = _run_facts(conn, run_id)
    pending = has_pending_decision(conn, run_id)
    owners = _owners(conn, run, [f.file_path for f in findings])
    ids = {
        (r["file_path"], r["line"], r["symbol"] or None): r["id"]
        for r in conn.execute(
            "SELECT id, file_path, line, symbol FROM impact_findings WHERE upgrade_run_id = ?", (run_id,)
        )
    }
    finding_rows = [
        {**f.model_dump(), "id": ids.get((f.file_path, f.line, f.symbol)), "owners": owners.get(f.file_path, [])}
        for f in findings
    ]
    signals = {
        s: sum(1 for f in findings if s in f.sources and f.confirmed is not False)
        for s in ("pattern", "runtime_deprecation", "test_failure")
    }
    probes = _probe_events(conn, run_id)
    return {
        "run": _run_summary(conn, run),
        "event": event,
        "findings": finding_rows,
        "telemetry": [t.model_dump() for t in run_store.load_telemetry(conn, run_id)],
        "plan": plan.model_dump() if plan else None,
        "patches": patches,
        "tests": {
            "current": probes.get("baseline_probe"),
            "unpatched": probes.get("upgrade_probe"),
            "patched": (patches[-1]["validation"] if patches else None),
        },
        "evidence": {
            "pattern_lines": signals["pattern"],
            "runtime_warnings": signals["runtime_deprecation"],
            "failing_places": signals["test_failure"],
            "production_lines": sum(t.count for t in run_store.load_telemetry(conn, run_id)),
        },
        "approval": dict(approval) if approval else None,
        "pull_request": dict(pr) if pr else None,
        "decision_pending": pending,
        "can_decide": run["status"] == "awaiting_approval" and not pending,
        "needs_engineer": bool(patches) and not facts["tests_pass"],
    }


def _owners(conn: sqlite3.Connection, run: sqlite3.Row, paths: list[str]) -> dict[str, list[str]]:
    source = run_store.scan_repo(conn, run["scan_id"])
    rules = load_codeowners(source["clone_path"])
    return {p: owners_for(p, rules) for p in set(paths)}


# --- blast radius graph ----------------------------------------

GRAPH_CAP = 150
_INTERNAL_IMPORTS = """
SELECT substr(i.source_id, 6) AS src, substr(r.target_id, 6) AS dst
FROM edges i
JOIN edges r ON r.source_id = i.target_id AND r.type = 'RESOLVES_TO' AND r.scan_id = i.scan_id
WHERE i.type = 'IMPORTS' AND i.scan_id = :scan AND r.target_id LIKE 'file:%' AND i.source_id LIKE 'file:%'
"""


def blast_radius(conn: sqlite3.Connection, scan_id: str, run_id: str) -> dict:
    run = run_store.get_run(conn, run_id)
    if run is None or run["scan_id"] != scan_id:
        raise NotFound(f"upgrade run {run_id} in scan {scan_id}")
    update = updates_store.get_update_row(conn, run["dependency_update_id"])
    dep_id = update["dep_node_id"]
    scope = dict(impact_store.package_scope(conn, scan_id, dep_id))
    findings = [f for f in run_store.load_findings(conn, run_id) if f.confirmed is not False]
    by_file: dict[str, int] = {}
    for f in findings:
        by_file[f.file_path] = by_file.get(f.file_path, 0) + 1
    for path in by_file:
        scope.setdefault(path, 1)
    paths = sorted(scope, key=lambda p: (-by_file.get(p, 0), scope[p], p))[: GRAPH_CAP - 2]
    keep = set(paths)
    rules = load_codeowners(run_store.scan_repo(conn, scan_id)["clone_path"])
    owners = {p: (owners_for(p, rules) or [None])[0] for p in paths}

    nodes = [
        {
            "id": f"dep:{update['name']}",
            "kind": "dependency",
            "label": f"{update['name']} {run['to_version']}",
            "status": "target",
            "findings": 0,
            "owner": None,
        }
    ]
    edges = []
    for p in paths:
        nodes.append(
            {
                "id": f"file:{p}",
                "kind": "file",
                "label": p,
                "status": "affected" if by_file.get(p) else "blast_radius",
                "findings": by_file.get(p, 0),
                "owner": owners[p],
            }
        )
        if scope[p] == 0:
            edges.append({"source": f"file:{p}", "target": f"dep:{update['name']}", "kind": "resolves_to"})
    for row in conn.execute(_INTERNAL_IMPORTS, {"scan": scan_id}):
        src, dst = row["src"].replace("\\", "/"), row["dst"].replace("\\", "/")
        if src in keep and dst in keep:
            edges.append({"source": f"file:{src}", "target": f"file:{dst}", "kind": "imports"})
    teams = sorted({o for o in owners.values() if o})
    for team in teams:
        nodes.append(
            {"id": f"team:{team}", "kind": "team", "label": team, "status": "normal", "findings": 0, "owner": team}
        )
    for p in paths:
        if owners[p]:
            edges.append({"source": f"team:{owners[p]}", "target": f"file:{p}", "kind": "owns"})
    return {"nodes": nodes, "edges": edges, "capped": len(scope) > len(paths)}


# --- health ----------------------------------------


def health(conn: sqlite3.Connection, llm_mode: str) -> dict:
    from app.core import db as core_db

    applied = core_db.applied_versions(conn)
    queued = (
        conn.execute("SELECT count(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
        if ("jobs" in core_db.table_names(conn))
        else 0
    )
    return {"status": "ok", "migrations": applied, "llm_mode": llm_mode, "jobs_queued": queued}


# --- writes ----------------------------------------


def save_feedback(
    conn: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: int,
    verdict: str,
    user_name: str | None,
    comment: str | None,
) -> int:
    table = "impact_findings" if target_kind == "impact_finding" else "update_changes"
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)).fetchone() is None:  # noqa: S608
        raise NotFound(f"{target_kind} {target_id}")
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback (target_kind, target_id, verdict, user_name, comment) VALUES (?, ?, ?, ?, ?)",
            (target_kind, target_id, verdict, user_name, comment),
        )
    return cur.lastrowid


def run_status(conn: sqlite3.Connection, run_id: str) -> str:
    run = run_store.get_run(conn, run_id)
    if run is None:
        raise NotFound(f"upgrade run {run_id}")
    return run["status"]


def has_pending_decision(conn: sqlite3.Connection, run_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'resume_upgrade' AND dedupe_key LIKE ? AND status IN ('queued','running')",
            (f"resume_upgrade:{run_id}:%",),
        ).fetchone()
        is not None
    )
