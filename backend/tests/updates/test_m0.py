import pytest

from app.updates import cli, store
from app.updates.preflight import PreflightError, run_preflight

from .conftest import make_db

NEW_TABLES = {
    "registry_packages", "registry_versions", "package_sources", "vulnerabilities",
    "update_analyses", "update_changes", "dependency_updates", "dependency_update_vulns",
    "release_notes", "pipeline_runs",
}


def test_migrate_twice(db_path):
    conn = store.connect(str(db_path))
    store.migrate(conn)
    store.migrate(conn)
    assert NEW_TABLES <= store._table_names(conn)
    assert store.migration_applied(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migrate_does_not_touch_ingestion_rows(db_path):
    conn = store.connect(str(db_path))
    before = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("scans", "nodes", "edges")]
    store.migrate(conn)
    after = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("scans", "nodes", "edges")]
    assert before == after


def test_migrate_needs_ingestion_tables(tmp_path):
    conn = store.connect(str(tmp_path / "empty.db"), must_exist=False)
    with pytest.raises(store.StoreError, match="Ingestion tables missing"):
        store.migrate(conn)


def test_connect_missing_file(tmp_path):
    with pytest.raises(store.StoreError):
        store.connect(str(tmp_path / "nope.db"))


def test_pragmas(db_path):
    conn = store.connect(str(db_path))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_latest_scan_skips_scans_without_nodes(db_path):
    conn = store.connect(str(db_path))
    assert store.latest_scan_id(conn) == "scan-new"


def test_latest_scan_tie_broken_by_rowid(tmp_path):
    p = make_db(tmp_path / "tie.db", patched=True)
    conn = store.connect(str(p))
    conn.execute("INSERT INTO scans (id, scanned_at) VALUES ('scan-tie', '2026-10-26 15:50:15')")
    conn.execute(
        "INSERT INTO nodes (id, scan_id, type, name, properties) VALUES ('dep:npm:x','scan-tie','dependency','x','{}')"
    )
    conn.commit()
    assert store.latest_scan_id(conn) == "scan-tie"


def test_inventory_order_and_counts(db_path):
    conn = store.connect(str(db_path))
    deps = store.load_inventory(conn, "scan-new")
    assert [d.name for d in deps] == ["supertest", "morgan", "eslint"]
    assert [d.importer_count for d in deps] == [3, 1, 0]
    assert deps[0].declared_constraint == "^6.3.0"
    assert deps[0].manifest_path == "package.json"
    assert deps[0].ecosystem == "npm"


def test_dependency_type_fallback_without_patch(original_db):
    deps = store.load_inventory(store.connect(str(original_db)), "scan-new")
    assert {d.dependency_type for d in deps} == {"unknown"}


def test_dependency_type_with_patch(patched_db):
    deps = {d.name: d for d in store.load_inventory(store.connect(str(patched_db)), "scan-new")}
    assert deps["supertest"].dependency_type == "dev"
    assert deps["morgan"].dependency_type == "runtime"


def test_importing_files_lines(patched_db):
    deps = {d.name: d for d in store.load_inventory(store.connect(str(patched_db)), "scan-new")}
    files = deps["supertest"].importing_files
    assert [f.path for f in files] == ["test/a.js", "test/b.js", "test/c.js"]
    assert files[0].lines == [1, 5]
    assert deps["morgan"].importing_files[0].lines == [4]
    assert deps["eslint"].importing_files == []


def test_importing_files_without_lines(original_db):
    deps = {d.name: d for d in store.load_inventory(store.connect(str(original_db)), "scan-new")}
    assert all(f.lines == [] for f in deps["supertest"].importing_files)


def test_backslash_paths_normalised(tmp_path):
    p = make_db(tmp_path / "bs.db", patched=False, backslashes=True)
    deps = {d.name: d for d in store.load_inventory(store.connect(str(p)), "scan-new")}
    assert [f.path for f in deps["supertest"].importing_files] == ["test/a.js", "test/b.js", "test/c.js"]


def test_preflight_report(db_path):
    conn = store.connect(str(db_path))
    report = run_preflight(conn)
    assert report.scan_id == "scan-new"
    assert report.dependencies == 3
    assert report.lockfiles == 0
    assert report.declared_without_constraint == 0
    assert report.missing_dependency_type in (0, 3)


def test_preflight_warns_missing_type_on_original(original_db):
    report = run_preflight(store.connect(str(original_db)))
    assert report.missing_dependency_type == 3
    assert any("dependency_type" in w for w in report.warnings)


def test_preflight_counts_lockfile(patched_db):
    conn = store.connect(str(patched_db))
    conn.execute(
        "INSERT INTO nodes (id, scan_id, type, name, properties) "
        "VALUES ('file:package-lock.json','scan-new','file','package-lock.json','{}')"
    )
    conn.commit()
    assert run_preflight(conn).lockfiles == 1


def test_preflight_fails_without_scan(tmp_path):
    p = tmp_path / "e.db"
    import sqlite3

    from .conftest import ORIGINAL_DDL

    c = sqlite3.connect(p)
    c.executescript(ORIGINAL_DDL)
    c.commit()
    c.close()
    with pytest.raises(PreflightError, match="No scan"):
        run_preflight(store.connect(str(p)))


def test_preflight_fails_without_dependencies(patched_db):
    conn = store.connect(str(patched_db))
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes WHERE type = 'dependency'")
    conn.commit()
    with pytest.raises(PreflightError, match="no dependency"):
        run_preflight(conn)


def test_undeclared_imports(patched_db):
    # 'redis' is a bare import that resolves to nothing; './x' is relative and ignored.
    assert store.count_undeclared_imports(store.connect(str(patched_db)), "scan-new") == 1


def test_cli_migrate_preflight_inventory(patched_db, capsys):
    assert cli.main(["migrate", "--db", str(patched_db)]) == 0
    assert cli.main(["preflight", "--db", str(patched_db)]) == 0
    assert cli.main(["inventory", "--db", str(patched_db)]) == 0
    out = capsys.readouterr().out
    assert "Dependencies:                   3" in out
    assert "supertest" in out


def test_cli_error_exit_code(tmp_path, capsys):
    assert cli.main(["preflight", "--db", str(tmp_path / "missing.db")]) == 1
    assert "ERROR" in capsys.readouterr().err
