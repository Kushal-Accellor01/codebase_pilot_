"""SQL for the scan layers: SBOM components, advisories with verdicts, scan requests and their stages (S1, S2)."""
import json
import sqlite3
import uuid

from app.scan.sbom import Component

VERDICT_RANK = {"applies": 0, "undetermined": 1, "does_not_apply": 2, "not_shipped": 3}
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

# --- SBOM ------------------------------------------------------------------------------------------------------------


def replace_components(conn: sqlite3.Connection, scan_id: str, components: list[Component]) -> None:
    with conn:
        conn.execute("DELETE FROM sbom_components WHERE scan_id = ?", (scan_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO sbom_components (scan_id, location, purl, name, version, scope, direct, path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(scan_id, c.location, c.purl, c.name, c.version, c.scope, int(c.direct), json.dumps(c.path))
             for c in components],
        )


def load_components(conn: sqlite3.Connection, scan_id: str) -> list[Component]:
    return [
        Component(purl=r["purl"], name=r["name"], version=r["version"], scope=r["scope"], direct=bool(r["direct"]),
                  path=json.loads(r["path"]), location=r["location"])
        for r in conn.execute("SELECT * FROM sbom_components WHERE scan_id = ? ORDER BY name, version, location",
                              (scan_id,))
    ]


def component_counts(conn: sqlite3.Connection, scan_id: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*) AS total, coalesce(sum(direct), 0) AS direct FROM sbom_components WHERE scan_id = ?",
        (scan_id,),
    ).fetchone()
    return {"total": row["total"], "direct": row["direct"]}


def layer1_counts(conn: sqlite3.Connection, scan_id: str) -> dict[str, int]:
    """What ingestion found: graph nodes, imports and declared packages."""
    row = conn.execute(
        """SELECT (SELECT count(*) FROM nodes WHERE scan_id = :s) AS nodes,
                  (SELECT count(*) FROM edges WHERE scan_id = :s AND type = 'IMPORTS') AS imports,
                  (SELECT count(*) FROM edges WHERE scan_id = :s AND type = 'DECLARES') AS declared""",
        {"s": scan_id},
    ).fetchone()
    return {"nodes": row["nodes"], "imports": row["imports"], "declared": row["declared"]}


def importing_package_names(conn: sqlite3.Connection, scan_id: str) -> list[tuple[str, str]]:
    """(dependency name, importing file path) for every IMPORTS edge that ends at a declared package."""
    from app.updates import store as updates_store

    out = []
    for dep in updates_store.load_inventory(conn, scan_id):
        for f in dep.importing_files:
            out.append((dep.name, f.path))
    return out


# --- advisories ---------------------------------------------------------------------------------------------------


def replace_scan_vulns(conn: sqlite3.Connection, scan_id: str, rows: list[dict]) -> None:
    """Replace the OSV-derived rows of a scan. Rows that Dependabot added (source list holds it) keep their entry."""
    with conn:
        conn.execute("DELETE FROM scan_vulns WHERE scan_id = ?", (scan_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO scan_vulns
                   (scan_id, vuln_id, name, version, purl, scope, direct, path, severity, summary, aliases, fixed_in,
                    verdict, reason, sources, url)
               VALUES (:scan_id, :vuln_id, :name, :version, :purl, :scope, :direct, :path, :severity, :summary,
                       :aliases, :fixed_in, :verdict, :reason, :sources, :url)""",
            [
                {**r, "scan_id": scan_id, "direct": int(r["direct"]), "path": json.dumps(r["path"]),
                 "aliases": json.dumps(r.get("aliases") or []), "sources": json.dumps(r["sources"])}
                for r in rows
            ],
        )


def insert_scan_vuln(conn: sqlite3.Connection, scan_id: str, row: dict) -> None:
    """Add one advisory row if (scan, advisory, package, version) is not there yet."""
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO scan_vulns
                   (scan_id, vuln_id, name, version, purl, scope, direct, path, severity, summary, aliases, fixed_in,
                    verdict, reason, sources, url)
               VALUES (:scan_id, :vuln_id, :name, :version, :purl, :scope, :direct, :path, :severity, :summary,
                       :aliases, :fixed_in, :verdict, :reason, :sources, :url)""",
            {**row, "scan_id": scan_id, "direct": int(row["direct"]), "path": json.dumps(row["path"]),
             "aliases": json.dumps(row.get("aliases") or []), "sources": json.dumps(row["sources"])},
        )


def list_scan_vulns(conn: sqlite3.Connection, scan_id: str) -> list[dict]:
    rows = []
    for r in conn.execute("SELECT * FROM scan_vulns WHERE scan_id = ?", (scan_id,)):
        d = dict(r)
        d["path"] = json.loads(d["path"])
        d["aliases"] = json.loads(d["aliases"] or "[]")
        d["sources"] = json.loads(d["sources"])
        d["direct"] = bool(d["direct"])
        rows.append(d)
    rows.sort(key=lambda d: (SEVERITY_RANK.get(d["severity"], 4), VERDICT_RANK.get(d["verdict"], 9), d["name"],
                             d["vuln_id"]))
    return rows


def add_vuln_source(conn: sqlite3.Connection, scan_id: str, vuln_id: str, name: str, source: str) -> bool:
    """Record that another source (Dependabot) also reports this advisory for this package. False: no such row."""
    row = conn.execute(
        "SELECT sources FROM scan_vulns WHERE scan_id = ? AND vuln_id = ? AND name = ?", (scan_id, vuln_id, name)
    ).fetchone()
    if row is None:
        return False
    sources = json.loads(row["sources"])
    if source not in sources:
        with conn:
            conn.execute(
                "UPDATE scan_vulns SET sources = ? WHERE scan_id = ? AND vuln_id = ? AND name = ?",
                (json.dumps([*sources, source]), scan_id, vuln_id, name),
            )
    return True


# --- scan requests and stages --------------------------------------------------------------------------------------


STAGES = ("clone", "layer1", "sbom", "internal", "layer2", "vulnerabilities", "versions", "changes", "impact")


def create_scan_request(
    conn: sqlite3.Connection, *, repo_url: str, branch: str | None, scope: dict, only: list[str] | None
) -> str:
    request_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO scan_requests (id, repo_url, branch, scope, only) VALUES (?, ?, ?, ?, ?)",
            (request_id, repo_url, branch, json.dumps(scope), json.dumps(only) if only else None),
        )
    return request_id


def set_request_job(conn: sqlite3.Connection, request_id: str, job_id: str) -> None:
    with conn:
        conn.execute("UPDATE scan_requests SET job_id = ? WHERE id = ?", (job_id, request_id))


def update_scan_request(
    conn: sqlite3.Connection, request_id: str, *, status: str | None = None, scan_id: str | None = None,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """UPDATE scan_requests SET status = COALESCE(?, status), scan_id = COALESCE(?, scan_id),
                      error = COALESCE(?, error), updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?""",
            (status, scan_id, error, request_id),
        )


def reopen_scan_request(conn: sqlite3.Connection, request_id: str) -> None:
    """A retry is running: the request is working again and the old error no longer applies."""
    with conn:
        conn.execute("UPDATE scan_requests SET status = 'running', error = NULL, "
                     "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?", (request_id,))


def get_scan_request(conn: sqlite3.Connection, request_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM scan_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["scope"] = json.loads(data["scope"])
    data["only"] = json.loads(data["only"]) if data["only"] else None
    return data


def request_for_scan(conn: sqlite3.Connection, scan_id: str) -> dict | None:
    row = conn.execute("SELECT id FROM scan_requests WHERE scan_id = ? ORDER BY created_at DESC LIMIT 1",
                       (scan_id,)).fetchone()
    return get_scan_request(conn, row["id"]) if row else None


def list_scan_requests(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute("SELECT id FROM scan_requests ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    return [get_scan_request(conn, r["id"]) for r in rows.fetchall()]


def add_scan_event(
    conn: sqlite3.Connection, request_id: str, stage: str, status: str, summary: str | None = None,
    data: dict | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO scan_events (request_id, stage, status, summary, data) VALUES (?, ?, ?, ?, ?)",
            (request_id, stage, status, summary, json.dumps(data) if data else None),
        )
    return cur.lastrowid


def list_scan_events(conn: sqlite3.Connection, request_id: str, after: int = 0) -> list[dict]:
    rows = []
    for r in conn.execute("SELECT * FROM scan_events WHERE request_id = ? AND id > ? ORDER BY id", (request_id, after)):
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        rows.append(d)
    return rows


def stage_summary(conn: sqlite3.Connection, request_id: str) -> list[dict]:
    """The latest event of every stage, in pipeline order; stages not reached yet are `queued`."""
    latest: dict[str, dict] = {}
    for event in list_scan_events(conn, request_id):
        latest[event["stage"]] = event
    return [
        {"stage": s, "status": latest[s]["status"] if s in latest else "queued",
         "summary": latest[s]["summary"] if s in latest else None, "data": latest[s]["data"] if s in latest else None}
        for s in STAGES
    ]
