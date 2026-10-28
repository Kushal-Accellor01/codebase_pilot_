"""A database that went through the real golden path once: the API tests read what the system produced."""

import asyncio
import shutil

import pytest
import respx

from app.core import db as core_db
from app.core import jobs
from app.orchestrator import cli as orch_cli
from app.orchestrator.worker import Services, run_worker
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from tests.conftest import make_sandbox_repo
from tests.e2e import golden


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.origin = make_sandbox_repo(tmp_path / "origin" / "pilot-sandbox-shop")
        self.db = str(tmp_path / "demo.db")
        for name, value in {
            "CODEBASE_PILOT_DB": self.db,
            "CODEBASE_PILOT_WORKSPACE": str(tmp_path / "ws"),
            "CHECKPOINT_DB": str(tmp_path / "ck.db"),
            "REPORTS_DIR": str(tmp_path / "reports"),
            "EXEC_ALLOWED_REPOS": str(self.origin),
            "PACKAGE_SOURCES_PATH": str(tmp_path / "sources.yaml"),
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("SEEDED_LOGS_DIR", raising=False)
        golden.install_fake_runner(monkeypatch)
        orch_cli._ensure_db(self.db)
        self.llm = golden.ScriptedLLM()

    def services(self, http) -> Services:
        return Services(
            http=http,
            registry=golden.registry(),
            github=GitHubClient(http, "t"),
            llm=self.llm,
            update_options={"security": False, "write_report": False},
        )

    def work(self, *new_jobs):
        async def main():
            async with HttpClient(wait_scale=0) as http:
                conn = core_db.connect(self.db)
                for kind, payload in new_jobs:
                    jobs.enqueue(conn, kind, payload)
                conn.close()
                return await run_worker(self.db, until_idle=True, services=self.services(http))

        with respx.mock:
            golden.mock_github()
            return asyncio.run(main())


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Scan the sandbox mirror and stop at the approval pause."""
    mp = pytest.MonkeyPatch()
    env = Env(tmp_path_factory.mktemp("api"), mp)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    yield env
    mp.undo()
    shutil.rmtree(env.tmp, ignore_errors=True)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """An isolated environment (tests that change state)."""
    env = Env(tmp_path, monkeypatch)
    env.work(("scan", {"repo_url": str(env.origin), "only": ["express"]}))
    return env
