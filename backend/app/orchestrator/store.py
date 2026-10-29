"""SQL for upgrade runs and everything a run produces (plan, patches, validations, approval, PR)."""
import json
import sqlite3
import uuid

from app.core.contracts import Finding, PatchResult, Plan, ProbeResult, TelemetryEvidence

RUN_STATUSES = (
    "queued", "analyzing", "fixing", "validating", "awaiting_approval", "approved", "rejected", "pr_opened",
    "needs_engineer", "issue_opened", "report_only", "failed", "resolved",
)


# --- runs ------------------------------------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection, *, scan_id: str, dependency_update_id: int, ecosystem: str, package: str,
    from_version: str | None, to_version: str | None, risk_level: str | None, status: str = "queued",
) -> str:
    """One run per (scan, dependency update). Calling again returns the existing run id."""
    existing = conn.execute(
        "SELECT id FROM upgrade_runs WHERE scan_id = ? AND dependency_update_id = ?",
        (scan_id, dependency_update_id),
    ).fetchone()
    if existing:
        return existing["id"]
    run_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            """INSERT INTO upgrade_runs
                   (id, scan_id, dependency_update_id, ecosystem, package, from_version, to_version, status,
                    risk_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, scan_id, dependency_update_id, ecosystem, package, from_version, to_version, status,
             risk_level),
        )
    return run_id


FINAL_STATUSES = ("rejected", "pr_opened", "needs_engineer", "issue_opened", "report_only", "failed", "resolved")


def clone_paths_in_use(conn: sqlite3.Connection) -> list[str]:
    """Clones that unfinished runs started on (their worktrees point into them)."""
    marks = ",".join("?" for _ in FINAL_STATUSES)
    return [
        r["clone_path"]
        for r in conn.execute(
            f"""SELECT DISTINCT m.clone_path FROM upgrade_runs r JOIN scan_meta m ON m.scan_id = r.scan_id
                WHERE r.status NOT IN ({marks}) AND m.clone_path IS NOT NULL""",  # noqa: S608
            FINAL_STATUSES,
        )
    ]


OPEN_STATUSES = (
    "queued", "analyzing", "fixing", "validating", "awaiting_approval", "approved", "pr_opened", "needs_engineer",
    "issue_opened", "report_only",
)


def find_open_run(
    conn: sqlite3.Connection, *, repo_url: str | None, ecosystem: str, package: str, to_version: str | None
) -> sqlite3.Row | None:
    """The newest run for the same repo, package and target that is still in play (not rejected, failed or resolved).

    A re-scan finds the same update again; reusing the run keeps one decision per upgrade."""
    marks = ",".join("?" for _ in OPEN_STATUSES)
    return conn.execute(
        f"""SELECT r.* FROM upgrade_runs r LEFT JOIN scan_meta m ON m.scan_id = r.scan_id
            WHERE r.ecosystem = ? AND r.package = ? AND r.to_version IS ? AND r.status IN ({marks})
              AND m.repo_url IS ? ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1""",  # noqa: S608
        (ecosystem, package, to_version, *OPEN_STATUSES, repo_url),
    ).fetchone()


def runs_for_scan(conn: sqlite3.Connection, scan_id: str) -> dict[int, sqlite3.Row]:
    """The run behind each dependency update of this scan.

    A re-scan does not start a second run for an upgrade that is already in play (runner.enqueue_runs_for_scan
    reuses the open run of the earlier scan), so that run is mapped onto the matching update of this scan.
    """
    from app.updates import store as updates_store

    runs = {
        r["dependency_update_id"]: r for r in conn.execute("SELECT * FROM upgrade_runs WHERE scan_id = ?", (scan_id,))
    }
    repo_url = scan_repo(conn, scan_id)["repo_url"]
    for update in updates_store.list_dependency_updates(conn, scan_id):
        if update["id"] in runs or not update["recommended_target"]:
            continue
        carried = find_open_run(
            conn, repo_url=repo_url, ecosystem=update["ecosystem"], package=update["name"],
            to_version=update["recommended_target"],
        )
        if carried is not None:
            runs[update["id"]] = carried
    return runs


def status_counts_for_scan(conn: sqlite3.Connection, scan_id: str) -> dict[str, int]:
    """Runs behind the scan's updates by status, carried runs included."""
    counts: dict[str, int] = {}
    for run in runs_for_scan(conn, scan_id).values():
        counts[run["status"]] = counts.get(run["status"], 0) + 1
    return counts


def known_repos(conn: sqlite3.Connection) -> list[str]:
    """Repository URLs that have been scanned, oldest first (what a scheduled re-scan walks)."""
    return [
        r["repo_url"]
        for r in conn.execute(
            "SELECT repo_url, min(rowid) AS first FROM scan_meta WHERE repo_url IS NOT NULL GROUP BY repo_url "
            "ORDER BY first"
        )
    ]


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM upgrade_runs WHERE id = ?", (run_id,)).fetchone()


def set_run_status(
    conn: sqlite3.Connection, run_id: str, status: str, *, node: str | None = None, error: str | None = None
) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    with conn:
        conn.execute(
            """UPDATE upgrade_runs SET status = ?, current_node = COALESCE(?, current_node), error = ?,
                      updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?""",
            (status, node, error, run_id),
        )


def list_runs(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM upgrade_runs WHERE status = ? ORDER BY created_at, rowid", (status,)
        ).fetchall()
    return conn.execute("SELECT * FROM upgrade_runs ORDER BY created_at, rowid").fetchall()


def scan_repo(conn: sqlite3.Connection, scan_id: str) -> dict:
    """Where a scan came from: repo_url / branch / clone path / commit. Works with and without the patch."""
    scan = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if scan is None:
        raise KeyError(scan_id)
    meta = conn.execute("SELECT * FROM scan_meta WHERE scan_id = ?", (scan_id,)).fetchone()
    data = dict(scan)
    return {
        "repo_url": data.get("repo_url") or (meta["repo_url"] if meta else None),
        "branch": data.get("branch") or (meta["branch"] if meta else None),
        "clone_path": (meta["clone_path"] if meta else None) or data.get("repository_path"),
        "commit_sha": data.get("commit_sha"),
    }


# --- impact findings ---------------------------------------------------------------------------------------------


def save_findings(conn: sqlite3.Connection, run_id: str, findings: list[Finding]) -> None:
    """Replace the run's findings. `source` holds the comma-joined signals; symbol '' means none."""
    with conn:
        conn.execute("DELETE FROM impact_findings WHERE upgrade_run_id = ?", (run_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO impact_findings
                   (upgrade_run_id, change_id, file_path, line, end_line, snippet, symbol, source, confidence,
                    confirmed, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, f.change_id, f.file_path, f.line, f.end_line, f.snippet, f.symbol or "",
                 ",".join(f.sources), f.confidence, None if f.confirmed is None else int(f.confirmed), f.reason)
                for f in findings
            ],
        )


def load_findings(conn: sqlite3.Connection, run_id: str, include_rejected: bool = True) -> list[Finding]:
    sql = "SELECT * FROM impact_findings WHERE upgrade_run_id = ?"
    if not include_rejected:
        sql += " AND (confirmed IS NULL OR confirmed = 1)"
    rows = conn.execute(sql + " ORDER BY file_path, line", (run_id,)).fetchall()
    return [
        Finding(
            file_path=r["file_path"], line=r["line"], end_line=r["end_line"], snippet=r["snippet"],
            symbol=r["symbol"] or None, change_id=r["change_id"], sources=r["source"].split(","),
            confidence=r["confidence"], confirmed=None if r["confirmed"] is None else bool(r["confirmed"]),
            reason=r["reason"] or "",
        )
        for r in rows
    ]


# --- telemetry -----------------------------------------------------------------------------------------------------


def save_telemetry(conn: sqlite3.Connection, run_id: str, evidence: list[TelemetryEvidence]) -> None:
    with conn:
        conn.execute("DELETE FROM telemetry_evidence WHERE upgrade_run_id = ?", (run_id,))
        conn.executemany(
            """INSERT INTO telemetry_evidence
                   (upgrade_run_id, source, signature, count, first_seen, last_seen, sample, related_symbol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(run_id, e.source, e.signature, e.count, e.first_seen, e.last_seen, e.sample, e.related_symbol)
             for e in evidence],
        )


def load_telemetry(conn: sqlite3.Connection, run_id: str) -> list[TelemetryEvidence]:
    return [
        TelemetryEvidence(
            source=r["source"], signature=r["signature"], count=r["count"], first_seen=r["first_seen"],
            last_seen=r["last_seen"], sample=r["sample"], related_symbol=r["related_symbol"],
        )
        for r in conn.execute(
            "SELECT * FROM telemetry_evidence WHERE upgrade_run_id = ? ORDER BY count DESC, id", (run_id,)
        )
    ]


# --- plan ------------------------------------------------------------------------------------------------------------


def save_plan(conn: sqlite3.Connection, run_id: str, plan: Plan) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO remediation_plans
                   (upgrade_run_id, severity, effort_hours, rationale, steps_json, validation_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, plan.severity, plan.effort_hours, plan.rationale, json.dumps(plan.steps),
             json.dumps(plan.validation)),
        )


def load_plan(conn: sqlite3.Connection, run_id: str) -> Plan | None:
    r = conn.execute("SELECT * FROM remediation_plans WHERE upgrade_run_id = ?", (run_id,)).fetchone()
    if r is None:
        return None
    return Plan(severity=r["severity"], effort_hours=r["effort_hours"], rationale=r["rationale"],
                steps=json.loads(r["steps_json"]), validation=json.loads(r["validation_json"]))


# --- patches and validations ------------------------------------------------------------------------------------------


def save_patch(conn: sqlite3.Connection, run_id: str, patch: PatchResult) -> int:
    """Insert (or replace) one attempt and its validation row; returns the patch id."""
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO patches
                   (upgrade_run_id, attempt, method, edits_json, diff, manifest_change, rejected_edits)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, patch.attempt, patch.method, json.dumps([e.model_dump() for e in patch.edits]), patch.diff,
             patch.manifest_change, json.dumps(patch.rejected)),
        )
        patch_id = conn.execute(
            "SELECT id FROM patches WHERE upgrade_run_id = ? AND attempt = ?", (run_id, patch.attempt)
        ).fetchone()["id"]
        conn.execute("DELETE FROM validations WHERE patch_id = ?", (patch_id,))
        if patch.validation:
            save_validation(conn, patch_id, patch.validation, commit=False)
    return patch_id


def save_validation(conn: sqlite3.Connection, patch_id: int, probe: ProbeResult, *, commit: bool = True) -> None:
    sql = """INSERT INTO validations
                 (patch_id, command, exit_code, passed, tests_passed, tests_total, duration_ms, output_tail)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
    args = (patch_id, probe.command, probe.exit_code, int(probe.passed), probe.tests_passed, probe.tests_total,
            probe.duration_ms, probe.output_tail)
    if commit:
        with conn:
            conn.execute(sql, args)
    else:
        conn.execute(sql, args)


def load_patches(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    out = []
    for p in conn.execute("SELECT * FROM patches WHERE upgrade_run_id = ? ORDER BY attempt", (run_id,)):
        v = conn.execute("SELECT * FROM validations WHERE patch_id = ? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        out.append({
            "id": p["id"], "attempt": p["attempt"], "method": p["method"], "diff": p["diff"],
            "edits": json.loads(p["edits_json"]), "manifest_change": p["manifest_change"],
            "rejected": json.loads(p["rejected_edits"] or "[]"),
            "validation": dict(v) if v else None,
        })
    return out


# --- approval and PR ----------------------------------------------------------------------------------------


def save_approval(
    conn: sqlite3.Connection, run_id: str, decision: str, decided_by: str, comment: str | None = None
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO approvals (upgrade_run_id, decision, decided_by, comment) VALUES (?, ?, ?, ?)",
            (run_id, decision, decided_by, comment),
        )


def load_approval(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM approvals WHERE upgrade_run_id = ?", (run_id,)).fetchone()


def save_pull_request(
    conn: sqlite3.Connection, run_id: str, *, kind: str, repo: str, number: int | None, url: str | None,
    branch: str | None, state: str | None,
) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO pull_requests (upgrade_run_id, kind, repo, number, url, branch, state)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, kind, repo, number, url, branch, state),
        )


def load_pull_request(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pull_requests WHERE upgrade_run_id = ?", (run_id,)).fetchone()


def runs_with_status(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM upgrade_runs WHERE status IN ({marks}) ORDER BY created_at", statuses  # noqa: S608
    ).fetchall()


def mark_pull_request_state(conn: sqlite3.Connection, run_id: str, state: str) -> None:
    with conn:
        conn.execute("UPDATE pull_requests SET state = ? WHERE upgrade_run_id = ?", (state, run_id))
