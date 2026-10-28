import json

from app.core import db as core_db
from app.orchestrator.ingest import ingest, repo_name
from app.updates import store


def test_repo_name():
    assert repo_name("https://github.com/acme/shop.git") == "shop"
    assert repo_name("C:\a\b\shop") == "shop"
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
    assert (tmp_path / "ws" / "pilot-sandbox-shop" / "routes" / "orders.js").exists()
    row = conn.execute("SELECT repository_path, commit_sha FROM scans WHERE id = ?", (scan_id,)).fetchone()
    assert len(row["commit_sha"]) == 40 and json.dumps(row["repository_path"])


def test_rescan_replaces_the_clone_and_adds_a_scan(sandbox_origin, tmp_path):
    db = tmp_path / "t.db"
    first = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    second = ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    assert first != second
    conn = core_db.connect(str(db))
    assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 2
