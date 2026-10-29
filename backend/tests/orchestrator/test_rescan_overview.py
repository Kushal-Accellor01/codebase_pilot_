"""A re-scan keeps the open run (runner.enqueue_runs_for_scan); the newest scan's dashboard must still show it."""
from app.api import queries
from app.core import db as core_db
from app.core.contracts import Finding
from app.orchestrator import store as run_store
from app.orchestrator.ingest import canonical_repo_url, ingest


def _express_update(conn, scan_id: str) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO dependency_updates (scan_id, dep_node_id, ecosystem, name, dependency_type,
                   declared_constraint, current_version, latest_version, recommended_target, update_type,
                   risk_level, status, importer_count)
               VALUES (?, 'dep:npm:express', 'npm', 'express', 'runtime', '4.21.2', '4.21.2', '5.1.0', '5.1.0',
                   'major', 'critical', 'update_available', 7)""",
            (scan_id,),
        ).lastrowid


def test_newest_scan_still_shows_the_run_waiting_from_an_earlier_scan(sandbox_origin, tmp_path):
    db = str(tmp_path / "t.db")
    first = ingest(str(sandbox_origin), db, tmp_path / "ws")
    conn = core_db.connect(db)
    run_id = run_store.create_run(
        conn, scan_id=first, dependency_update_id=_express_update(conn, first), ecosystem="npm", package="express",
        from_version="4.21.2", to_version="5.1.0", risk_level="critical",
    )
    run_store.set_run_status(conn, run_id, "awaiting_approval", node="validate")
    run_store.save_findings(conn, run_id, [Finding(
        file_path="routes/orders.js", line=8, snippet="res.send(404)", sources=["pattern"], confidence=0.9,
    )])

    second = ingest(str(sandbox_origin), db, tmp_path / "ws")  # "Scan now" while the run waits
    _express_update(conn, second)

    overview = queries.overview(conn, second)
    assert [w["run_id"] for w in overview["waiting"]] == [run_id]
    row = next(u for u in overview["upgrades"] if u["package"] == "express")
    assert row["run_id"] == run_id and row["status_label"] == "Awaiting approval"
    dep = next(d for d in queries.list_dependencies(conn, second) if d["name"] == "express")
    assert dep["run_id"] == run_id and dep["reachability"] == "reachable_in_code"


def test_canonical_repo_url():
    assert canonical_repo_url("https://github.com/acme/shop.git") == "https://github.com/acme/shop"
    assert canonical_repo_url("https://github.com/acme/shop/") == "https://github.com/acme/shop"
    assert canonical_repo_url("git@github.com:acme/shop.git") == "git@github.com:acme/shop"
    assert canonical_repo_url("/work/origin/shop.git/") == "/work/origin/shop.git"  # local path: as given
