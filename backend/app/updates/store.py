"""All SQL for the dependency-updates step lives here.

Reads the ingestion tables (scans, nodes, edges) and never writes to them.
Works with and without ingestion-fixes.patch.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.updates.config import MIGRATIONS_DIR
from app.updates.models import Dependency, ImportingFile, SourceRepo, UpdateRow, VulnRecord
from app.updates.registry.base import PackageInfo, VersionInfo

LOCKFILE_NAMES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
)

INGESTION_TABLES = ("scans", "nodes", "edges")


class StoreError(RuntimeError):
    pass


def connect(db_path: str, *, must_exist: bool = True) -> sqlite3.Connection:
    if must_exist and not Path(db_path).is_file():
        raise StoreError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def posix(path: str) -> str:
    return path.replace("\\", "/")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def migrate(conn: sqlite3.Connection) -> None:
    """Apply 002_dependency_updates.sql. Idempotent (IF NOT EXISTS everywhere)."""
    missing = [t for t in INGESTION_TABLES if t not in _table_names(conn)]
    if missing:
        raise StoreError(
            f"Ingestion tables missing: {', '.join(missing)}. Run a scan first (python -m app.main <url>)."
        )
    sql = (MIGRATIONS_DIR / "002_dependency_updates.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


def migration_applied(conn: sqlite3.Connection) -> bool:
    return "dependency_updates" in _table_names(conn)


def latest_scan_id(conn: sqlite3.Connection) -> str | None:
    """Latest scan that actually has nodes (older ingestion overwrote nodes of earlier scans)."""
    row = conn.execute(
        """
        SELECT s.id
        FROM scans s
        WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.scan_id = s.id)
        ORDER BY s.scanned_at DESC, s.rowid DESC
        LIMIT 1
        """
    ).fetchone()
    return row["id"] if row else None


def scan_scanned_at(conn: sqlite3.Connection, scan_id: str) -> str:
    """Raw scans.scanned_at text (UTC, 'YYYY-MM-DD HH:MM:SS')."""
    row = conn.execute("SELECT scanned_at FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if row is None:
        raise StoreError(f"Scan not found: {scan_id}")
    return row["scanned_at"]


def scan_exists(conn: sqlite3.Connection, scan_id: str) -> bool:
    return conn.execute("SELECT 1 FROM scans WHERE id = ?", (scan_id,)).fetchone() is not None


def _count(conn: sqlite3.Connection, sql: str, scan_id: str) -> int:
    return conn.execute(sql, {"scan": scan_id}).fetchone()[0]


def count_dependencies(conn: sqlite3.Connection, scan_id: str) -> int:
    return _count(
        conn, "SELECT count(*) FROM nodes WHERE type = 'dependency' AND scan_id = :scan", scan_id
    )


def count_declares_without_constraint(conn: sqlite3.Connection, scan_id: str) -> int:
    return _count(
        conn,
        """SELECT count(*) FROM edges
           WHERE type = 'DECLARES' AND scan_id = :scan
             AND json_extract(properties, '$.declared_constraint') IS NULL""",
        scan_id,
    )


def count_declares_without_type(conn: sqlite3.Connection, scan_id: str) -> int:
    return _count(
        conn,
        """SELECT count(*) FROM edges
           WHERE type = 'DECLARES' AND scan_id = :scan
             AND json_extract(properties, '$.dependency_type') IS NULL""",
        scan_id,
    )


def count_lockfiles(conn: sqlite3.Connection, scan_id: str) -> int:
    placeholders = ",".join("?" for _ in LOCKFILE_NAMES)
    return conn.execute(
        f"SELECT count(*) FROM nodes WHERE type = 'file' AND scan_id = ? AND name IN ({placeholders})",  # noqa: S608
        (scan_id, *LOCKFILE_NAMES),
    ).fetchone()[0]


def count_undeclared_imports(conn: sqlite3.Connection, scan_id: str) -> int:
    """External-looking modules that resolve to nothing (run stat 'undeclared_imports')."""
    return _count(
        conn,
        """SELECT count(*) FROM nodes m
           WHERE m.type = 'module' AND m.scan_id = :scan
             AND m.name NOT LIKE '.%' AND m.name NOT LIKE '/%'
             AND NOT EXISTS (SELECT 1 FROM edges r
                             WHERE r.source_id = m.id AND r.scan_id = m.scan_id
                               AND r.type = 'RESOLVES_TO')""",
        scan_id,
    )


_INVENTORY_SQL = """
SELECT d.id                                                   AS dep_node_id,
       d.name,
       json_extract(d.properties, '$.ecosystem')              AS ecosystem,
       decl.source_id                                         AS manifest_file_id,
       json_extract(decl.properties, '$.declared_constraint') AS declared_constraint,
       json_extract(decl.properties, '$.dependency_type')     AS dependency_type,
       json_extract(decl.properties, '$.marker')              AS marker,
       (SELECT count(DISTINCT i.source_id)
          FROM edges r
          JOIN edges i ON i.target_id = r.source_id
                      AND i.type = 'IMPORTS'
                      AND i.scan_id = r.scan_id
         WHERE r.type = 'RESOLVES_TO'
           AND r.target_id = d.id
           AND r.scan_id = :scan)                             AS importer_count
FROM nodes d
LEFT JOIN edges decl ON decl.target_id = d.id
                    AND decl.type = 'DECLARES'
                    AND decl.scan_id = :scan
WHERE d.type = 'dependency'
  AND d.scan_id = :scan
ORDER BY importer_count DESC, d.name
"""

_IMPORTERS_SQL = """
SELECT r.target_id                                AS dep_node_id,
       i.source_id                                AS file_id,
       json_extract(i.properties, '$.lines')      AS lines
FROM edges r
JOIN edges i ON i.target_id = r.source_id
            AND i.type = 'IMPORTS'
            AND i.scan_id = r.scan_id
WHERE r.type = 'RESOLVES_TO'
  AND r.scan_id = :scan
  AND r.target_id LIKE 'dep:%'
ORDER BY 1, 2
"""


def _file_path(file_id: str) -> str:
    return posix(file_id[5:] if file_id.startswith("file:") else file_id)


def importing_files_by_dep(conn: sqlite3.Connection, scan_id: str) -> dict[str, list[ImportingFile]]:
    """dep node id -> files that import it, with line numbers when ingestion recorded them."""
    merged: dict[str, dict[str, set[int]]] = {}
    for row in conn.execute(_IMPORTERS_SQL, {"scan": scan_id}):
        lines = json.loads(row["lines"]) if row["lines"] else []
        files = merged.setdefault(row["dep_node_id"], {})
        files.setdefault(_file_path(row["file_id"]), set()).update(int(n) for n in lines)
    return {
        dep: [ImportingFile(path=p, lines=sorted(ls)) for p, ls in sorted(files.items())]
        for dep, files in merged.items()
    }


def _repo_to_text(repo: str | dict | None) -> str | None:
    """repository_url holds the raw registry value: JSON text for the {url, directory} form."""
    if repo is None:
        return None
    return json.dumps(repo) if isinstance(repo, dict) else repo


def _repo_from_text(text: str | None) -> str | dict | None:
    if text and text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def save_registry_package(conn: sqlite3.Connection, info: PackageInfo) -> None:
    """Upsert one package and all its versions in a single short transaction."""
    with conn:
        conn.execute(
            """
            INSERT INTO registry_packages
                (ecosystem, name, latest_version, repository_url, homepage_url, is_deprecated,
                 deprecation_message, registry_modified, etag, status, error, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(ecosystem, name) DO UPDATE SET
                latest_version = excluded.latest_version, repository_url = excluded.repository_url,
                homepage_url = excluded.homepage_url, is_deprecated = excluded.is_deprecated,
                deprecation_message = excluded.deprecation_message,
                registry_modified = excluded.registry_modified, etag = excluded.etag,
                status = excluded.status, error = NULL, fetched_at = excluded.fetched_at
            """,
            (
                info.ecosystem, info.name, info.latest_version, _repo_to_text(info.repository),
                info.homepage, int(info.is_deprecated), info.deprecation_message,
                info.registry_modified, info.etag, info.status,
            ),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO registry_versions
                (ecosystem, name, version, published_at, is_prerelease, deprecation_message,
                 engines_json, peer_dependencies_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    info.ecosystem, info.name, v.version, v.published_at, int(v.is_prerelease),
                    v.deprecation_message, json.dumps(v.engines), json.dumps(v.peer_dependencies),
                )
                for v in info.versions
            ],
        )


def save_registry_failure(conn: sqlite3.Connection, ecosystem: str, name: str, error: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO registry_packages (ecosystem, name, status, error)
            VALUES (?, ?, 'error', ?)
            ON CONFLICT(ecosystem, name) DO UPDATE SET
                status = 'error', error = excluded.error,
                fetched_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (ecosystem, name, error),
        )


def load_registry_package(
    conn: sqlite3.Connection, ecosystem: str, name: str
) -> tuple[PackageInfo, datetime] | None:
    """Cached package and the time it was fetched, or None."""
    row = conn.execute(
        "SELECT * FROM registry_packages WHERE ecosystem = ? AND name = ?", (ecosystem, name)
    ).fetchone()
    if row is None:
        return None
    versions = [
        VersionInfo(
            version=v["version"],
            published_at=v["published_at"],
            is_prerelease=bool(v["is_prerelease"]),
            deprecation_message=v["deprecation_message"],
            engines=json.loads(v["engines_json"] or "{}"),
            peer_dependencies=json.loads(v["peer_dependencies_json"] or "{}"),
        )
        for v in conn.execute(
            "SELECT * FROM registry_versions WHERE ecosystem = ? AND name = ?", (ecosystem, name)
        )
    ]
    info = PackageInfo(
        ecosystem=ecosystem,
        name=name,
        status=row["status"],
        latest_version=row["latest_version"],
        versions=versions,
        repository=_repo_from_text(row["repository_url"]),
        homepage=row["homepage_url"],
        registry_modified=row["registry_modified"],
        etag=row["etag"],
        is_deprecated=bool(row["is_deprecated"]),
        deprecation_message=row["deprecation_message"],
    )
    fetched_at = datetime.strptime(row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return info, fetched_at


def load_inventory(conn: sqlite3.Connection, scan_id: str) -> list[Dependency]:
    importers = importing_files_by_dep(conn, scan_id)
    deps: list[Dependency] = []
    for row in conn.execute(_INVENTORY_SQL, {"scan": scan_id}):
        files = importers.get(row["dep_node_id"], [])
        manifest_id = row["manifest_file_id"] or ""
        deps.append(
            Dependency(
                dep_node_id=row["dep_node_id"],
                ecosystem=row["ecosystem"] or "unknown",
                name=row["name"],
                manifest_file_id=manifest_id,
                manifest_path=_file_path(manifest_id) if manifest_id else "",
                declared_constraint=row["declared_constraint"],
                dependency_type=row["dependency_type"] or "unknown",
                marker=row["marker"],
                importer_count=row["importer_count"],
                importing_files=files,
            )
        )
    return deps


_UPSERT_COLUMNS = (
    "ecosystem", "name", "dependency_type", "declared_constraint", "current_version", "current_version_basis",
    "latest_in_range", "latest_version", "recommended_target", "update_type", "requires_manifest_change",
    "versions_behind", "runtime_requirement_change", "is_deprecated", "deprecation_message", "vuln_count",
    "max_severity", "min_safe_version", "importer_count", "priority_score", "risk_level", "status", "error",
)


def upsert_dependency_update(conn: sqlite3.Connection, row: UpdateRow) -> int:
    """One short transaction; returns the row id. analysis_id is left alone: it belongs to the analysis step."""
    data = row.model_dump()
    for key in ("requires_manifest_change", "runtime_requirement_change", "is_deprecated"):
        data[key] = int(data[key])
    columns = ("scan_id", "dep_node_id", "manifest_file_id", *_UPSERT_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _UPSERT_COLUMNS)
    with conn:
        conn.execute(
            f"""INSERT INTO dependency_updates ({", ".join(columns)}, checked_at)
                VALUES ({placeholders}, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                ON CONFLICT(scan_id, dep_node_id, manifest_file_id) DO UPDATE SET
                    {updates}, checked_at = excluded.checked_at""",  # noqa: S608
            data,
        )
        return conn.execute(
            "SELECT id FROM dependency_updates WHERE scan_id = ? AND dep_node_id = ? AND manifest_file_id = ?",
            (row.scan_id, row.dep_node_id, row.manifest_file_id),
        ).fetchone()["id"]


def list_dependency_updates(conn: sqlite3.Connection, scan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM dependency_updates WHERE scan_id = ? ORDER BY priority_score DESC, name", (scan_id,)
    ).fetchall()


def start_pipeline_run(conn: sqlite3.Connection, run_id: str, scan_id: str, step: str = "dependency_updates") -> None:
    with conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, scan_id, step, status) VALUES (?, ?, ?, 'running')",
            (run_id, scan_id, step),
        )


def finish_pipeline_run(
    conn: sqlite3.Connection, run_id: str, status: str, stats: dict | None = None, error: str | None = None
) -> None:
    with conn:
        conn.execute(
            """UPDATE pipeline_runs SET status = ?, stats = ?, error = ?,
                   finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?""",
            (status, json.dumps(stats) if stats is not None else None, error, run_id),
        )


def get_pipeline_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()


def load_vulnerability(conn: sqlite3.Connection, vuln_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM vulnerabilities WHERE id = ?", (vuln_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["raw"] = json.loads(data["raw"])
    return data


def save_vulnerability(conn: sqlite3.Connection, rec: VulnRecord) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO vulnerabilities
                (id, aliases, summary, severity, cvss_vector, published_at, modified_at, url, raw, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(id) DO UPDATE SET
                aliases = excluded.aliases, summary = excluded.summary, severity = excluded.severity,
                cvss_vector = excluded.cvss_vector, published_at = excluded.published_at,
                modified_at = excluded.modified_at, url = excluded.url, raw = excluded.raw,
                fetched_at = excluded.fetched_at
            """,
            (
                rec.id, json.dumps(rec.aliases), rec.summary, rec.severity, rec.cvss_vector,
                rec.published_at, rec.modified_at, rec.url, json.dumps(rec.raw),
            ),
        )


def replace_update_vulns(conn: sqlite3.Connection, update_id: int, vulns: list[tuple[str, str | None]]) -> None:
    """Set the (vuln_id, fixed_in) links of one dependency_updates row."""
    with conn:
        conn.execute("DELETE FROM dependency_update_vulns WHERE update_id = ?", (update_id,))
        conn.executemany(
            "INSERT INTO dependency_update_vulns (update_id, vuln_id, fixed_in) VALUES (?, ?, ?)",
            [(update_id, vid, fixed) for vid, fixed in vulns],
        )


def list_update_vulns(conn: sqlite3.Connection, update_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT v.id, v.severity, v.summary, v.url, l.fixed_in
           FROM dependency_update_vulns l JOIN vulnerabilities v ON v.id = l.vuln_id
           WHERE l.update_id = ? ORDER BY v.id""",
        (update_id,),
    ).fetchall()


def load_package_source(
    conn: sqlite3.Connection, ecosystem: str, name: str
) -> tuple[SourceRepo, datetime] | None:
    row = conn.execute(
        "SELECT * FROM package_sources WHERE ecosystem = ? AND name = ?", (ecosystem, name)
    ).fetchone()
    if row is None:
        return None
    source = SourceRepo(
        owner=row["owner"] or "",
        repo=row["repo"] or "",
        directory=row["directory"],
        default_branch=row["default_branch"],
        archived=bool(row["archived"]),
        changelog_path=row["changelog_path"],
        tag_format=row["tag_format"],
        method=row["method"],
        confidence=row["confidence"],
        verified=bool(row["verified"]),
        status=row["status"],
    )
    resolved_at = datetime.strptime(row["resolved_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return source, resolved_at


def save_package_source(conn: sqlite3.Connection, ecosystem: str, name: str, source: SourceRepo) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO package_sources
                (ecosystem, name, host, owner, repo, directory, default_branch, archived, tag_format,
                 changelog_path, method, confidence, verified, status, resolved_at)
            VALUES (?, ?, 'github', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(ecosystem, name) DO UPDATE SET
                host = excluded.host, owner = excluded.owner, repo = excluded.repo,
                directory = excluded.directory, default_branch = excluded.default_branch,
                archived = excluded.archived, tag_format = excluded.tag_format,
                changelog_path = excluded.changelog_path, method = excluded.method,
                confidence = excluded.confidence, verified = excluded.verified, status = excluded.status,
                resolved_at = excluded.resolved_at
            """,
            (
                ecosystem, name, source.owner, source.repo, source.directory, source.default_branch,
                int(source.archived), source.tag_format, source.changelog_path, source.method,
                source.confidence, int(source.verified), source.status,
            ),
        )


def remember_changelog_path(conn: sqlite3.Connection, ecosystem: str, name: str, path: str) -> None:
    with conn:
        conn.execute(
            "UPDATE package_sources SET changelog_path = ? WHERE ecosystem = ? AND name = ?",
            (path, ecosystem, name),
        )
