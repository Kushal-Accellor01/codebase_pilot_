import sqlite3
import json
import uuid
from typing import Dict, Any, List

class GraphDatabase:
    def __init__(self, db_path: str = "codebase_pilot.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                repository_path TEXT,
                commit_sha TEXT,
                scanned_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                scan_id TEXT,
                type TEXT,
                name TEXT,
                properties JSON,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            )
        """)
        # ADDED: properties JSON to edges table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                source_id TEXT,
                target_id TEXT,
                type TEXT,
                scan_id TEXT,
                properties JSON,
                PRIMARY KEY (source_id, target_id, type, scan_id),
                FOREIGN KEY(source_id) REFERENCES nodes(id),
                FOREIGN KEY(target_id) REFERENCES nodes(id),
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            )
        """)
        self.conn.commit()

    def create_scan(self, repo_path: str, commit_sha: str = "HEAD") -> str:
        scan_id = str(uuid.uuid4())
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO scans (id, repository_path, commit_sha) VALUES (?, ?, ?)",
            (scan_id, repo_path, commit_sha)
        )
        self.conn.commit()
        return scan_id

    def add_node(self, node_id: str, scan_id: str, node_type: str, name: str, properties: Dict[str, Any]):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO nodes (id, scan_id, type, name, properties) VALUES (?, ?, ?, ?, ?)",
            (node_id, scan_id, node_type, name, json.dumps(properties))
        )

    # ADDED: properties argument to edges
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