import asyncio
import json
from pathlib import Path

from app.orchestrator.ingest import ingest
from app.updates import pipeline, store
from app.updates.lockfile import load_lockfile_versions
from tests.updates.test_umt7l import FakeRegistry, info

SANDBOX = Path(__file__).resolve().parents[3] / "pilot-sandbox-shop"


def test_sandbox_lockfile_gives_express_4_21_2():
    versions = load_lockfile_versions(SANDBOX)
    assert versions["express"] == "4.21.2" and versions["mocha"] == "10.8.2"
    assert not any("/" in name and not name.startswith("@") for name in versions)  # nested copies are ignored


def test_nested_node_modules_entries_are_ignored(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "x"},
            "node_modules/a": {"version": "1.0.0"},
            "node_modules/a/node_modules/b": {"version": "9.9.9"},
            "node_modules/@scope/pkg": {"version": "2.0.0"},
            "node_modules/c": {"resolved": "no version"},
        },
    }), encoding="utf-8")
    assert load_lockfile_versions(tmp_path) == {"a": "1.0.0", "@scope/pkg": "2.0.0"}


def test_v1_lockfile(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 1, "dependencies": {"a": {"version": "1.2.3", "dependencies": {"b": {"version": "0.0.1"}}}},
    }), encoding="utf-8")
    assert load_lockfile_versions(tmp_path) == {"a": "1.2.3"}


def test_shrinkwrap_missing_and_broken_files(tmp_path):
    assert load_lockfile_versions(tmp_path) == {} and load_lockfile_versions(None) == {}
    (tmp_path / "npm-shrinkwrap.json").write_text(json.dumps({"packages": {"node_modules/z": {"version": "3.0.0"}}}), encoding="utf-8")
    assert load_lockfile_versions(tmp_path) == {"z": "3.0.0"}
    (tmp_path / "npm-shrinkwrap.json").write_text("{not json", encoding="utf-8")
    assert load_lockfile_versions(tmp_path) == {}


def test_pipeline_uses_the_lockfile_basis(sandbox_origin, tmp_path):
    db = tmp_path / "t.db"
    ingest(str(sandbox_origin), str(db), tmp_path / "ws")
    registry = FakeRegistry({
        "express": info("express", ["4.21.2", "5.3.0"]),
        "mocha": info("mocha", ["10.8.2"]),
        "supertest": info("supertest", ["6.3.4"]),
    })
    asyncio.run(pipeline.run_dependency_updates_async(
        str(db), registry=registry, security=False, notes=False, write_report=False))
    conn = store.connect(str(db))
    rows = {r["name"]: r for r in store.list_dependency_updates(conn, store.latest_scan_id(conn))}
    assert rows["express"]["current_version"] == "4.21.2" and rows["express"]["current_version_basis"] == "lockfile"
    assert rows["express"]["update_type"] == "major"
