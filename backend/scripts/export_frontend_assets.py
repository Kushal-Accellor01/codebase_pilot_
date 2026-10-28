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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "frontend"))
    out = Path(parser.parse_args(argv).out)

    write(out / "openapi.json", create_app().openapi())

    with tempfile.TemporaryDirectory() as tmp:
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
            conn = core_db.connect(env.db)
            conn.close()
        finally:
            mp.undo()
    print(f"wrote {out / 'openapi.json'} and fixtures under {out / 'src' / 'api' / 'fixtures'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
