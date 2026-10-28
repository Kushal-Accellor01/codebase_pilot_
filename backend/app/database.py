import sqlite3
import json
import uuid
from typing import Dict, Any, Optional


class GraphDatabase:
    def __init__(self, db_path: str = "codebase_pilot.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Wait instead of failing immediately if another process (e.g. DB Browser) holds a lock.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                repository_path TEXT,
                commit_sha TEXT,
                scanned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                repo_url TEXT,
                branch TEXT
            )
        """)
        # Node identity is per scan: the same file or dependency id can exist in many scans.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT,
                scan_id TEXT,
                type TEXT,
                name TEXT,
                properties JSON,
                PRIMARY KEY (scan_id, id),
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                source_id TEXT,
                target_id TEXT,
                type TEXT,
                scan_id TEXT,
                properties JSON,
                PRIMARY KEY (source_id, target_id, type, scan_id),
                FOREIGN KEY(scan_id, source_id) REFERENCES nodes(scan_id, id),
                FOREIGN KEY(scan_id, target_id) REFERENCES nodes(scan_id, id),
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            )
        """)
        self._migrate(cursor)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_scan_type ON nodes(scan_id, type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_scan_type ON edges(scan_id, type)")
        self.conn.commit()

    def _migrate(self, cursor):
        """Upgrade databases created by the first version of this schema (safe to run every time)."""
        scan_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(scans)")}
        for col in ("repo_url", "branch"):
            if col not in scan_cols:
                cursor.execute(f"ALTER TABLE scans ADD COLUMN {col} TEXT")

        node_pk = [row["name"] for row in sorted(cursor.execute("PRAGMA table_info(nodes)"), key=lambda r: r["pk"]) if row["pk"] > 0]
        if node_pk == ["id"]:
            # Old schema: nodes keyed by id alone, so every re-scan overwrote earlier scans' nodes.
            cursor.executescript("""
                BEGIN;
                ALTER TABLE nodes RENAME TO nodes_v1;
                ALTER TABLE edges RENAME TO edges_v1;
                CREATE TABLE nodes (
                    id TEXT, scan_id TEXT, type TEXT, name TEXT, properties JSON,
                    PRIMARY KEY (scan_id, id),
                    FOREIGN KEY(scan_id) REFERENCES scans(id)
                );
                CREATE TABLE edges (
                    source_id TEXT, target_id TEXT, type TEXT, scan_id TEXT, properties JSON,
                    PRIMARY KEY (source_id, target_id, type, scan_id),
                    FOREIGN KEY(scan_id, source_id) REFERENCES nodes(scan_id, id),
                    FOREIGN KEY(scan_id, target_id) REFERENCES nodes(scan_id, id),
                    FOREIGN KEY(scan_id) REFERENCES scans(id)
                );
                INSERT INTO nodes (id, scan_id, type, name, properties)
                    SELECT id, scan_id, type, name, properties FROM nodes_v1;
                -- Keep only edges whose both ends exist in the same scan; edges of overwritten
                -- scans pointed at nodes that no longer belong to them.
                INSERT INTO edges (source_id, target_id, type, scan_id, properties)
                    SELECT e.source_id, e.target_id, e.type, e.scan_id, e.properties
                    FROM edges_v1 e
                    WHERE EXISTS (SELECT 1 FROM nodes n WHERE n.scan_id = e.scan_id AND n.id = e.source_id)
                      AND EXISTS (SELECT 1 FROM nodes n WHERE n.scan_id = e.scan_id AND n.id = e.target_id);
                DROP TABLE edges_v1;
                DROP TABLE nodes_v1;
                COMMIT;
            """)

    def create_scan(self, repo_path: str, commit_sha: str = "HEAD",
                    repo_url: Optional[str] = None, branch: Optional[str] = None) -> str:
        scan_id = str(uuid.uuid4())
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO scans (id, repository_path, commit_sha, repo_url, branch) VALUES (?, ?, ?, ?, ?)",
            (scan_id, repo_path, commit_sha, repo_url, branch)
        )
        self.conn.commit()
        return scan_id

    def add_node(self, node_id: str, scan_id: str, node_type: str, name: str, properties: Dict[str, Any]):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO nodes (id, scan_id, type, name, properties) VALUES (?, ?, ?, ?, ?)",
            (node_id, scan_id, node_type, name, json.dumps(properties))
        )

    def add_edge(self, source_id: str, target_id: str, edge_type: str, scan_id: str, properties: Dict[str, Any] = None):
        if properties is None:
            properties = {}
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO edges (source_id, target_id, type, scan_id, properties) VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, edge_type, scan_id, json.dumps(properties))
        )

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()
