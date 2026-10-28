import json
from pathlib import Path

from app.core import db as core_db
from app.orchestrator import store as run_store
from app.orchestrator.ingest import clone_key, ingest, repo_name
from app.updates import store


def test_repo_name():
    assert repo_name("https://github.com/acme/shop.git") == "shop"
    assert repo_name(r"C:\a\b\shop") == "shop"
    assert repo_name("/x/y/shop/") == "shop"


def test_ingest_sandbox_returns_scan_with_nodes_and_keeps_clone(sandbox_origin, tmp_path):
    db = tmp_path / "t.db"
    scan_id = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    conn = core_db.connect(str(db))
    assert store.latest_scan_id(conn) == scan_id
    deps = {d.name: d for d in store.load_inventory(conn, scan_id)}
    assert deps["express"].declared_constraint == "4.21.2" and deps["express"].importer_count >= 3
    assert {"mocha", "supertest"} <= set(deps)
    files = {f.path for f in deps["express"].importing_files}
    assert {"routes/orders.js", "routes/invoices.js", "app.js"} <= files
    assert "lib/notify.js" not in files
    meta = conn.execute("SELECT * FROM scan_meta WHERE scan_id = ?", (scan_id,)).fetchone()
    assert meta["repo_url"] == str(sandbox_origin).rstrip("/")
    clone = Path(meta["clone_path"])
    assert (tmp_path / "ws" / "clones").resolve() in clone.parents and (clone / "routes" / "orders.js").exists()
    row = conn.execute("SELECT repository_path, commit_sha FROM scans WHERE id = ?", (scan_id,)).fetchone()
    assert len(row["commit_sha"]) == 40 and json.dumps(row["repository_path"])


def test_clone_key_includes_the_owner():
    assert clone_key("https://github.com/acme/shop.git") == "acme__shop"
    assert clone_key("https://github.com/other/shop") == "other__shop"
    assert clone_key("shop") == "local__shop"


def test_rescan_adds_a_scan_and_prunes_clones_nobody_needs(sandbox_origin, tmp_path):
    db = tmp_path / "t.db"
    first = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    second = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    assert first != second
    conn = core_db.connect(str(db))
    assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 2
    clones = list((tmp_path / "ws" / "clones").glob("*/*"))
    assert len(clones) == 1  # no unfinished run uses the first clone, so only the newest stays


def test_clone_of_an_unfinished_run_survives_a_rescan(sandbox_origin, tmp_path):
    db = tmp_path / "t.db"
    first = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    conn = core_db.connect(str(db))
    node_id = conn.execute("SELECT id FROM nodes WHERE scan_id = ? LIMIT 1", (first,)).fetchone()["id"]
    with conn:
        update_id = conn.execute(
            "INSERT INTO dependency_updates (scan_id, dep_node_id, ecosystem, name) VALUES (?, ?, 'npm', 'express')",
            (first, node_id),
        ).lastrowid
    run_store.create_run(conn, scan_id=first, dependency_update_id=update_id, ecosystem="npm", package="express",
                         from_version="4.21.2", to_version="5.3.0", risk_level="high", status="awaiting_approval")
    ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    assert len(list((tmp_path / "ws" / "clones").glob("*/*"))) == 2
