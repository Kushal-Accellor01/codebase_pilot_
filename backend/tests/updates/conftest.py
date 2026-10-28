"""Fixture DBs in both ingestion schema shapes (original dc31a53 and patched)."""
import json
import sqlite3
from pathlib import Path

import pytest

ORIGINAL_DDL = """
CREATE TABLE scans (
    id TEXT PRIMARY KEY, repository_path TEXT, commit_sha TEXT,
    scanned_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE nodes (
    id TEXT PRIMARY KEY, scan_id TEXT, type TEXT, name TEXT, properties JSON,
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
CREATE TABLE edges (
    source_id TEXT, target_id TEXT, type TEXT, scan_id TEXT, properties JSON,
    PRIMARY KEY (source_id, target_id, type, scan_id),
    FOREIGN KEY(source_id) REFERENCES nodes(id),
    FOREIGN KEY(target_id) REFERENCES nodes(id),
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
"""

PATCHED_DDL = """
CREATE TABLE scans (
    id TEXT PRIMARY KEY, repository_path TEXT, commit_sha TEXT,
    scanned_at DATETIME DEFAULT CURRENT_TIMESTAMP, repo_url TEXT, branch TEXT
);
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
CREATE INDEX idx_nodes_scan_type ON nodes(scan_id, type);
CREATE INDEX idx_edges_scan_type ON edges(scan_id, type);
"""


def _node(c, scan, nid, ntype, name, props):
    c.execute(
        "INSERT INTO nodes (id, scan_id, type, name, properties) VALUES (?,?,?,?,?)",
        (nid, scan, ntype, name, json.dumps(props)),
    )


def _edge(c, scan, src, dst, etype, props=None):
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, scan_id, properties) VALUES (?,?,?,?,?)",
        (src, dst, etype, scan, json.dumps(props or {})),
    )


def populate(c: sqlite3.Connection, scan: str, *, patched: bool, backslashes: bool = False) -> None:
    """supertest (3 importers, dev), morgan (1, runtime), eslint (0, dev), lockless package.json."""
    sep = "\\" if backslashes else "/"

    def dep_props(constraint: str, dep_type: str) -> dict:
        p = {"declared_constraint": constraint}
        if patched:
            p["dependency_type"] = dep_type
        return p

    _node(c, scan, "file:package.json", "file", "package.json", {"path": "package.json", "is_manifest": True})
    for name, constraint, dtype in (
        ("supertest", "^6.3.0", "dev"),
        ("morgan", "1.10.0", "runtime"),
        ("eslint", "^8.0.0", "dev"),
    ):
        _node(c, scan, f"dep:npm:{name}", "dependency", name, {"ecosystem": "npm"})
        _edge(c, scan, "file:package.json", f"dep:npm:{name}", "DECLARES", dep_props(constraint, dtype))

    files = [f"test{sep}a.js", f"test{sep}b.js", f"test{sep}c.js", f"lib{sep}app.js"]
    for f in files:
        _node(c, scan, f"file:{f}", "file", f.split(sep)[-1], {"path": f})

    _node(c, scan, "mod:supertest", "module", "supertest", {"is_import": True, "language": "javascript"})
    _node(c, scan, "mod:morgan", "module", "morgan", {"is_import": True, "language": "javascript"})
    _node(c, scan, "mod:redis", "module", "redis", {"is_import": True, "language": "javascript"})  # undeclared
    _node(c, scan, "mod:./x", "module", "./x", {"is_import": True, "language": "javascript"})
    _edge(c, scan, "mod:supertest", "dep:npm:supertest", "RESOLVES_TO")
    _edge(c, scan, "mod:morgan", "dep:npm:morgan", "RESOLVES_TO")

    for i, f in enumerate(files[:3]):
        props = {"lines": [i + 1, i + 5], "specifiers": ["supertest"]} if patched else {}
        _edge(c, scan, f"file:{f}", "mod:supertest", "IMPORTS", props)
    _edge(c, scan, f"file:{files[3]}", "mod:morgan", "IMPORTS", {"lines": [4]} if patched else {})
    _edge(c, scan, f"file:{files[3]}", "mod:redis", "IMPORTS")
    _edge(c, scan, f"file:{files[3]}", "mod:./x", "IMPORTS")


def make_db(path: Path, *, patched: bool, backslashes: bool = False) -> Path:
    c = sqlite3.connect(path)
    c.executescript(PATCHED_DDL if patched else ORIGINAL_DDL)
    # Older scan with no nodes (the original ingestion bug), then the real one.
    c.execute("INSERT INTO scans (id, scanned_at) VALUES ('scan-old', '2026-10-26 15:00:00')")
    c.execute("INSERT INTO scans (id, scanned_at) VALUES ('scan-new', '2026-10-26 15:50:15')")
    if patched:
        # A newer scan without nodes must still be skipped.
        c.execute("INSERT INTO scans (id, scanned_at) VALUES ('scan-empty', '2026-10-27 09:00:00')")
    populate(c, "scan-new", patched=patched, backslashes=backslashes)
    c.commit()
    c.close()
    return path


@pytest.fixture(params=["original", "patched"])
def db_path(request, tmp_path) -> Path:
    return make_db(tmp_path / "t.db", patched=request.param == "patched")


@pytest.fixture
def patched_db(tmp_path) -> Path:
    return make_db(tmp_path / "p.db", patched=True)


@pytest.fixture
def original_db(tmp_path) -> Path:
    return make_db(tmp_path / "o.db", patched=False)
