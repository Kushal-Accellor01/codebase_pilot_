"""B4: the job worker. claim -> dispatch by kind -> complete, or retry with backoff.

Job kinds
  scan                  {repo_url, only?}        ingest, then queue dependency_updates
  dependency_updates    {scan_id, only?}         Step 2, then queue an upgrade_run per dependency needing attention
  upgrade_run           {run_id}                 run the graph to the approval pause
  resume_upgrade        {run_id, decision, decided_by, comment?}
"""
import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field

from app.core import db as core_db
from app.core import jobs
from app.core.config import Settings, get_settings
from app.core.llm import LLM
from app.orchestrator import runner
from app.orchestrator.ingest import ingest
from app.orchestrator.nodes import Publisher
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from app.updates.pipeline import run_dependency_updates_async
from app.updates.registry.base import RegistryAdapter

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything a job may need, injectable so tests never touch the network."""

    http: HttpClient | None = None
    registry: RegistryAdapter | None = None
    github: GitHubClient | None = None
    llm: LLM | None = None
    publisher: Publisher | None = None
    graph_overrides: dict | None = None
    update_options: dict = field(default_factory=dict)  # e.g. {"security": False} for offline runs


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def dispatch(job: jobs.Job, db_path: str, settings: Settings, services: Services) -> None:
    conn = core_db.connect(db_path)
    try:
        payload = job.payload
        if job.kind == "scan":
            scan_id = await asyncio.to_thread(ingest, payload["repo_url"], db_path, settings.codebase_pilot_workspace)
            jobs.enqueue(conn, "dependency_updates", {"scan_id": scan_id, "only": payload.get("only")},
                         dedupe_key=f"dependency_updates:{scan_id}")
        elif job.kind == "dependency_updates":
            scan_id = payload["scan_id"]
            await run_dependency_updates_async(
                db_path, scan_id, only=payload.get("only"), registry=services.registry, http=services.http,
                github=services.github, llm=services.llm, **services.update_options,
            )
            runner.enqueue_runs_for_scan(conn, scan_id, only=payload.get("only"))
        elif job.kind == "upgrade_run":
            ctx = runner.make_context(conn, services.http, settings, llm=services.llm, publisher=services.publisher)
            await runner.start_run(ctx, payload["run_id"], overrides=services.graph_overrides)
        elif job.kind == "resume_upgrade":
            ctx = runner.make_context(conn, services.http, settings, llm=services.llm, publisher=services.publisher)
            await runner.resume_run(
                ctx, payload["run_id"], payload["decision"], payload["decided_by"], payload.get("comment"),
                overrides=services.graph_overrides,
            )
        else:
            raise ValueError(f"unknown job kind {job.kind!r}")
    finally:
        conn.close()


async def run_worker(
    db_path: str | None = None, *, until_idle: bool = False, services: Services | None = None,
    settings: Settings | None = None, poll_s: float = 1.0, stop: asyncio.Event | None = None,
) -> int:
    """Process jobs. With until_idle=True return when no job can run any more; returns jobs handled."""
    settings = settings or get_settings()
    db_path = db_path or settings.codebase_pilot_db
    owns_http = services is None or services.http is None
    http = HttpClient() if owns_http else services.http
    services = services or Services()
    if services.http is None:
        services.http = http
    name = worker_name()
    handled = 0
    conn = core_db.connect(db_path)
    try:
        core_db.migrate(conn)
        recovered = jobs.recover_stuck(conn)
        if recovered:
            logger.info("re-queued %d stuck jobs", recovered)
        while not (stop and stop.is_set()):
            job = jobs.claim(conn, name)
            if job is None:
                if until_idle and jobs.runnable_count(conn) == 0:
                    break
                await asyncio.sleep(poll_s if not until_idle else min(poll_s, 0.2))
                continue
            handled += 1
            try:
                await dispatch(job, db_path, settings, services)
                jobs.complete(conn, job.id)
            except Exception as exc:
                logger.exception("job %s (%s) failed", job.id, job.kind)
                # A retry is scheduled for later (backoff), so it is not runnable now and until_idle can end.
                jobs.fail(conn, job, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
        if owns_http:
            await http.aclose()
    return handled
