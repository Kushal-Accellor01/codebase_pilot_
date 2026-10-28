"""SQL for impact analysis: the package scope query (BS 7.1 step 2) and change lookups."""
import sqlite3

SCOPE_CAP = 300

_SCOPE_SQL = """
WITH RECURSIVE
importers(file_id) AS (
    SELECT DISTINCT i.source_id
    FROM edges r
    JOIN edges i ON i.target_id = r.source_id AND i.type = 'IMPORTS' AND i.scan_id = r.scan_id
    WHERE r.type = 'RESOLVES_TO' AND r.scan_id = :scan AND r.target_id = :dep
),
scope(file_id, depth) AS (
    SELECT file_id, 0 FROM importers
    UNION
    SELECT r.target_id, s.depth + 1
    FROM scope s
    JOIN edges i ON i.source_id = s.file_id AND i.type = 'IMPORTS' AND i.scan_id = :scan
    JOIN edges r ON r.source_id = i.target_id AND r.type = 'RESOLVES_TO' AND r.scan_id = :scan
    WHERE r.target_id LIKE 'file:%' AND s.depth < 3
)
SELECT substr(file_id, 6) AS path, min(depth) AS depth
FROM scope GROUP BY file_id ORDER BY depth, path
"""


def package_scope(conn: sqlite3.Connection, scan_id: str, dep_node_id: str) -> list[tuple[str, int]]:
    """(path, depth): files importing the package (depth 0) plus the internal files they import, 3 hops."""
    rows = conn.execute(_SCOPE_SQL, {"scan": scan_id, "dep": dep_node_id}).fetchall()
    return [(r["path"].replace("\\", "/"), r["depth"]) for r in rows]


def dependency_node_id(conn: sqlite3.Connection, dependency_update_id: int) -> str | None:
    row = conn.execute("SELECT dep_node_id FROM dependency_updates WHERE id = ?", (dependency_update_id,)).fetchone()
    return row["dep_node_id"] if row else None


def change_symbols(conn: sqlite3.Connection, change_ids: list[int]) -> dict[int, dict]:
    """update_changes rows by id (symbols and description are what the matchers need)."""
    if not change_ids:
        return {}
    marks = ",".join("?" for _ in change_ids)
    rows = conn.execute(f"SELECT * FROM update_changes WHERE id IN ({marks})", change_ids).fetchall()  # noqa: S608
    return {r["id"]: dict(r) for r in rows}
