"""The whole product, offline, for rehearsals and the browser smoke test (F1).

    python scripts/demo_server.py [--port 8000] [--dir workspace/offline-demo]

Seeds a scan of the sandbox mirror with a scripted model, recorded GitHub/npm data and the test output captured
from the real sandbox, stops the express run at the approval pause, then serves the API with the worker inside
the process. Approving in the dashboard opens a local pull request (a branch in the mirror plus a Markdown body).
No keys, no network. It is not a substitute for a real run: `scripts/dev.py seed-demo` does that.
"""
import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.orchestrator.worker import Services  # noqa: E402
from tests.api.conftest import Env  # noqa: E402
from tests.e2e import golden  # noqa: E402


def _writable(func, target, _exc):
    os.chmod(target, stat.S_IWRITE)
    func(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dir", default=str(ROOT / "backend" / "workspace" / "offline-demo"))
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    if directory.exists():
        shutil.rmtree(directory, onexc=_writable) if sys.version_info >= (3, 12) else shutil.rmtree(directory)
    directory.mkdir(parents=True)

    mp = pytest.MonkeyPatch()  # the env vars and the fake npm stay in force while the server runs
    env = Env(directory, mp)
    env.work(("scan", {"repo_url": str(env.origin)}))
    print(f"seeded {env.db}; mirror at {env.origin}", flush=True)

    settings = Settings(_env_file=None, codebase_pilot_db=env.db, pilot_embedded_worker=True)
    services = Services(llm=env.llm, registry=golden.registry(), update_options={"security": False, "write_report": False})
    uvicorn.run(create_app(env.db, settings, services), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
