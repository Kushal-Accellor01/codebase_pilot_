"""Regenerate what the frontend builds on: the OpenAPI document and fixture JSON (E1).

    python scripts/export_frontend_assets.py [--out ../frontend]

* `frontend/openapi.json`            -> `npm run gen:types` turns it into src/api/types.ts
* `frontend/src/api/fixtures/*.json` -> real API responses from the offline golden path (fixture mode and tests)

The fixtures come from the same scripted run as the backend tests: the sandbox mirror, recorded GitHub data and
the output captured from the real sandbox. No network, no model.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.core import db as core_db  # noqa: E402
from app.core.config import Settings  # noqa: E402
from tests.api.conftest import Env  # noqa: E402


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sse_events(client: TestClient, url: str) -> list[dict]:
    events, current = [], {}
    with client.stream("GET", url) as response:
        for line in response.iter_lines():
            if not line:
                if current.get("event") == "end":
                    break
                if current.get("event") == "run_event":
                    events.append({"id": int(current["id"]), **json.loads(current["data"])})
                current = {}
                continue
            key, _, value = line.partition(": ")
            current[key] = value
    return events


SAMPLE_ADVISORIES = (
    # Advisory ids and texts are labelled SAMPLE: the offline run has no network, so there are no real advisories.
    ("SAMPLE-1", "qs", "high", "Resource exhaustion on deeply nested input", "applies"),
    ("SAMPLE-2", "debug", "medium", "Expensive pattern in a formatting helper", "does_not_apply"),
    ("SAMPLE-3", "form-data", "medium", "Predictable boundary in multipart bodies", "not_shipped"),
    ("SAMPLE-4", "brace-expansion", "low", "Slow expansion of crafted patterns", "undetermined"),
)


def write_scan_fixtures(env, fixtures: Path) -> None:
    """Fixtures for the scan, results, sources, channels and update-feed pages."""
    from app.scan import store as scan_store

    conn = core_db.connect(env.db)
    scan_id = conn.execute("SELECT id FROM scans ORDER BY scanned_at DESC, rowid DESC LIMIT 1").fetchone()["id"]
    by_name = {c.name: c for c in scan_store.load_components(conn, scan_id)}
    rows = []
    for vuln_id, name, severity, summary, verdict in SAMPLE_ADVISORIES:
        component = by_name[name]
        reason = {"applies": f"Your code reaches it through {component.path[0]}.",
                  "does_not_apply": "No call path from your code to the affected function.",
                  "not_shipped": "Used only by tests; production builds leave it out.",
                  "undetermined": "Reached through a dynamic call Pilot cannot follow."}[verdict]
        rows.append({"vuln_id": vuln_id, "name": name, "version": component.version, "purl": component.purl,
                     "scope": component.scope, "direct": component.direct, "path": component.path,
                     "severity": severity, "summary": summary, "aliases": [], "fixed_in": None, "verdict": verdict,
                     "reason": reason, "sources": ["osv"], "url": None})
    scan_store.replace_scan_vulns(conn, scan_id, rows)
    conn.close()
    settings = Settings(_env_file=None, codebase_pilot_db=env.db, exec_allowed_repos=str(env.origin))
    with TestClient(create_app(env.db, settings)) as client:
        accepted = client.post("/api/scans", json={"repo_url": str(env.origin)}).json()
    env.work()  # runs the scan request above
    with TestClient(create_app(env.db, settings)) as client:
        request_id = accepted["scan_request_id"]
        scan = client.get(f"/api/scan-requests/{request_id}").json()
        scan_id = scan["scan_id"]
        scan_store.replace_scan_vulns(core_db.connect(env.db), scan_id, rows)  # sample advisories for the new scan
        write(fixtures / "scan_request.json", scan)
        write(fixtures / "repos.json", client.get("/api/repos").json())
        write(fixtures / "summary.json", client.get(f"/api/scans/{scan_id}/summary").json())
        write(fixtures / "vulnerabilities.json", client.get(f"/api/scans/{scan_id}/vulnerabilities").json())
        write(fixtures / "sources.json", client.get("/api/scans/latest/sources").json())
        repo_id = client.get("/api/repos").json()[0]["repo_id"]
        write(fixtures / "channels.json", client.get(f"/api/repos/{repo_id}/channels").json())
        write(fixtures / "changes.json", client.get("/api/changes").json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "frontend"))
    out = Path(parser.parse_args(argv).out)

    write(out / "openapi.json", create_app().openapi())

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mp = pytest.MonkeyPatch()
        try:
            env = Env(Path(tmp), mp)
            env.work(("scan", {"repo_url": str(env.origin)}))
            app = create_app(env.db, Settings(_env_file=None, codebase_pilot_db=env.db))
            with TestClient(app) as client:
                fixtures = out / "src" / "api" / "fixtures"
                scans = client.get("/api/scans").json()
                dependencies = client.get("/api/scans/latest/dependencies").json()
                upgrades = client.get("/api/upgrades").json()
                run_id = upgrades[0]["id"]
                write(fixtures / "scans.json", scans)
                write(fixtures / "overview.json", client.get("/api/scans/latest/overview").json())
                write(fixtures / "dependencies.json", dependencies)
                first = dependencies[0]["id"]
                write(fixtures / "dependency.json", client.get(f"/api/dependency-updates/{first}").json())
                write(fixtures / "upgrades.json", upgrades)
                write(fixtures / "upgrade.json", client.get(f"/api/upgrades/{run_id}").json())
                write(fixtures / "graph.json", client.get(f"/api/scans/latest/graph?run_id={run_id}").json())
                write(fixtures / "events.json", sse_events(client, f"/api/upgrades/{run_id}/events"))
            write_scan_fixtures(env, out / "src" / "api" / "fixtures")
        finally:
            mp.undo()
    print(f"wrote {out / 'openapi.json'} and fixtures under {out / 'src' / 'api' / 'fixtures'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
