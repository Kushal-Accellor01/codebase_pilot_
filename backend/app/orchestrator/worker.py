"""B4: the job worker. claim -> dispatch by kind -> complete, or retry with backoff.

Job kinds
  scan                  {repo_url, only?, branch?, scope?, request_id?}   ingest, then queue dependency_updates
  dependency_updates    {scan_id, only?, scope?, request_id?}   SBOM and advisories, Step 2, then an upgrade_run per
                                                 dependency needing attention
  upgrade_run           {run_id}                 run the graph to the approval pause
  resume_upgrade        {run_id, decision, decided_by, comment?}
  repo_actions          {scan_id}                comment on bot PRs, publish the dashboard and team issues (token only)
"""
import asyncio
import logging
import os
import socket
from dataclasses import dataclass, field

from app.actions.repo_actions import run_repo_actions
from app.core import db as core_db
from app.core import jobs
from app.core.config import Settings, get_settings
from app.core.llm import LLM
from app.orchestrator import push_tracker, runner
from app.orchestrator import store as run_store
from app.orchestrator.ingest import ingest
from app.orchestrator.nodes import Publisher
from app.scan import pipeline as scan_pipeline
from app.scan import store as scan_store
from app.updates import store as updates_store
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


async def _scan(conn, job: jobs.Job, db_path: str, settings: Settings) -> None:
    """Clone and read the structure (Layer 1), then hand the scan to `dependency_updates`."""
    payload = job.payload
    request_id = payload.get("request_id")
    stages = scan_pipeline.Stages(conn, request_id)
    if stages.cancelled():
        return
    if request_id:
        scan_store.update_scan_request(conn, request_id, status="running")
    branch = f" ({payload['branch']})" if payload.get("branch") else ""
    stages("clone", "running", f"Cloning {payload['repo_url']}{branch}")
    try:
        scan_id = await asyncio.to_thread(
            ingest, payload["repo_url"], db_path, settings.codebase_pilot_workspace, payload.get("branch"))
    except Exception as exc:
        stages("clone", "error", f"{type(exc).__name__}: {exc}"[:300])
        if request_id and job.attempts >= job.max_attempts:
            scan_store.update_scan_request(conn, request_id, status="failed", error=str(exc)[:300])
        raise
    source = run_store.scan_repo(conn, scan_id)
    where = source["branch"] or "the default branch"
    stages("clone", "done", f"Cloned at commit {(source['commit_sha'] or '')[:7]} on {where}")
    counts = scan_store.layer1_counts(conn, scan_id)
    stages("layer1", "done", f"{counts['nodes']} graph nodes, {counts['imports']} imports traced, "
                             f"{counts['declared']} packages declared", counts)
    if request_id:
        scan_store.update_scan_request(conn, request_id, scan_id=scan_id)
    jobs.enqueue(conn, "dependency_updates",
                 {"scan_id": scan_id, "only": payload.get("only"), "scope": payload.get("scope"),
                  "request_id": request_id},
                 dedupe_key=f"dependency_updates:{scan_id}")


async def _dependency_updates(conn, job: jobs.Job, db_path: str, settings: Settings, services: Services) -> None:
    """SBOM and advisories, versions and release notes, then one upgrade run per dependency that needs attention."""
    payload = job.payload
    scan_id = payload["scan_id"]
    scope = scan_pipeline.scope_of(payload.get("scope"))
    stages = scan_pipeline.Stages(conn, payload.get("request_id"))
    if stages.cancelled():
        return
    offline = services.update_options.get("security") is False
    await scan_pipeline.run_layers(
        conn, services.http, settings, scan_id, scope, stages, vulnerabilities_enabled=not offline)
    if stages.cancelled():
        return

    async def versions() -> str:
        options = dict(services.update_options)
        await run_dependency_updates_async(
            db_path, scan_id, only=payload.get("only"), registry=services.registry, http=services.http,
            github=services.github, llm=services.llm if scope["breaking_changes"] else None, **options,
        )
        rows = updates_store.list_dependency_updates(conn, scan_id)
        newer = sum(1 for r in rows if r["recommended_target"])
        return f"{len(rows)} dependencies checked, {newer} have a newer version"

    await stages.guard("versions", versions)
    if scope["breaking_changes"]:
        rows = updates_store.list_dependency_updates(conn, scan_id)
        read = sum(1 for r in rows if r["status"] == "analyzed")
        stages("changes", "done", f"{read} upgrades with release notes read")
    else:
        stages("changes", "skipped", "Not selected for this scan")
    if stages.cancelled():
        return

    pr_states = None
    if settings.github_token and services.http is not None:
        pr_states = await push_tracker.fetch_pr_states(conn, GitHubClient(services.http))
    push_tracker.close_resolved_runs(conn, scan_id, pr_states)
    queued = runner.enqueue_runs_for_scan(conn, scan_id, only=payload.get("only"))
    if queued:
        stages("impact", "running", f"{len(queued)} upgrade{'s' if len(queued) != 1 else ''} are being analysed")
    scan_pipeline.finish_if_done(conn, scan_id)
    if settings.github_token and settings.pilot_repo_actions:  # bot-PR comments, dashboard and team issues
        jobs.enqueue(conn, "repo_actions", {"scan_id": scan_id}, dedupe_key=f"repo_actions:{scan_id}")


async def dispatch(job: jobs.Job, db_path: str, settings: Settings, services: Services) -> None:
    conn = core_db.connect(db_path)
    try:
        payload = job.payload
        if job.kind == "scan":
            await _scan(conn, job, db_path, settings)
        elif job.kind == "dependency_updates":
            await _dependency_updates(conn, job, db_path, settings, services)
        elif job.kind == "repo_actions":
            await run_repo_actions(conn, payload["scan_id"], settings, services.http)
        elif job.kind == "upgrade_run":
            ctx = runner.make_context(conn, services.http, settings, llm=services.llm, publisher=services.publisher)
            await runner.start_run(ctx, payload["run_id"], overrides=services.graph_overrides)
            run = run_store.get_run(conn, payload["run_id"])
            if run is not None:
                scan_pipeline.finish_if_done(conn, run["scan_id"])
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
