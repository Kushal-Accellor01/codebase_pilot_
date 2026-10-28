"""B3: start, resume and enqueue upgrade runs.

One LangGraph thread per run. `start_run` is safe to call twice (it continues an interrupted thread, reports a
paused one, and leaves a finished one alone). `resume_run` only works while the run awaits approval.
"""
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.core import events, jobs
from app.core.config import Settings, get_settings
from app.core.llm import LLM
from app.orchestrator import graph as graph_module
from app.orchestrator import nodes
from app.orchestrator import store as run_store
from app.updates import store as updates_store
from app.updates.events import wants_event
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)


class RunConflict(RuntimeError):
    """The requested transition is not allowed in the run's current state (HTTP 409)."""


def build_llm(conn: sqlite3.Connection, http: HttpClient | None, settings: Settings) -> LLM | None:
    """An LLM only when one can answer: Azure configured, or replay mode (answers come from the cache)."""
    configured = bool(settings.azure_openai_endpoint and settings.azure_openai_deployment_strong)
    if http is None or not (configured or settings.llm_mode == "replay"):
        return None
    return LLM(conn, http, settings)


def enqueue_runs_for_scan(conn: sqlite3.Connection, scan_id: str, *, only: list[str] | None = None) -> list[str]:
    """A run and a job per dependency that needs attention (risk medium or higher, or a person must help).

    Safe to repeat: runs are unique per (scan, update) and jobs per event.
    """
    run_ids: list[str] = []
    wanted = set(only or [])
    repo_url = run_store.scan_repo(conn, scan_id)["repo_url"]
    for row in updates_store.list_dependency_updates(conn, scan_id):
        if not wants_event(row) or (wanted and row["name"] not in wanted):
            continue
        if not row["current_version"] or not row["recommended_target"]:
            continue
        open_run = run_store.find_open_run(
            conn, repo_url=repo_url, ecosystem=row["ecosystem"], package=row["name"], to_version=row["recommended_target"]
        )
        if open_run is not None and open_run["scan_id"] != scan_id:
            continue  # an earlier scan already has this upgrade in play: no second run, no repeated LLM calls
        run_id = run_store.create_run(
            conn, scan_id=scan_id, dependency_update_id=row["id"], ecosystem=row["ecosystem"], package=row["name"],
            from_version=row["current_version"], to_version=row["recommended_target"], risk_level=row["risk_level"],
        )
        event_id = f"{scan_id}:{row['ecosystem']}:{row['name']}:{row['current_version']}->{row['recommended_target']}"
        jobs.enqueue(conn, "upgrade_run", {"run_id": run_id}, dedupe_key=f"upgrade_run:{event_id}")
        run_ids.append(run_id)
    return run_ids


@asynccontextmanager
async def open_graph(ctx: nodes.RunContext, checkpoint_db: str | None = None, overrides: dict | None = None):
    path = checkpoint_db or ctx.settings.checkpoint_db
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield graph_module.build_graph(ctx, saver, overrides)


def make_context(
    conn: sqlite3.Connection, http: HttpClient | None, settings: Settings | None = None, *,
    llm: LLM | None = None, publisher: nodes.Publisher | None = None,
) -> nodes.RunContext:
    settings = settings or get_settings()
    return nodes.RunContext(
        conn=conn, http=http, llm=llm if llm is not None else build_llm(conn, http, settings), settings=settings,
        workspace_root=Path(settings.codebase_pilot_workspace).resolve(), publisher=publisher,
    )


async def start_run(ctx: nodes.RunContext, run_id: str, *, overrides: dict | None = None) -> str:
    """Run the graph until it pauses for approval or ends. Returns the run's status."""
    if run_store.get_run(ctx.conn, run_id) is None:
        raise KeyError(f"unknown upgrade run {run_id}")
    cfg = graph_module.thread_config(run_id)
    async with open_graph(ctx, overrides=overrides) as graph:
        snapshot = await graph.aget_state(cfg)
        try:
            if not snapshot.values:
                await graph.ainvoke({"run_id": run_id}, cfg)
            elif snapshot.next and tuple(snapshot.next) != ("approval",):
                await graph.ainvoke(None, cfg)  # continue after a crash from the last checkpoint
        except Exception as exc:
            logger.exception("upgrade run %s failed", run_id, extra={"run_id": run_id})
            run = run_store.get_run(ctx.conn, run_id)
            if run["status"] != "failed":
                run_store.set_run_status(ctx.conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
    return run_store.get_run(ctx.conn, run_id)["status"]


async def resume_run(
    ctx: nodes.RunContext, run_id: str, decision: str, decided_by: str, comment: str | None = None, *,
    overrides: dict | None = None,
) -> str:
    """Apply the human decision. Raises RunConflict unless the run is awaiting approval."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    run = run_store.get_run(ctx.conn, run_id)
    if run is None:
        raise KeyError(f"unknown upgrade run {run_id}")
    if run["status"] != "awaiting_approval":
        raise RunConflict(f"run {run_id} is {run['status']}, not awaiting approval")
    run_store.save_approval(ctx.conn, run_id, decision, decided_by, comment)
    run_store.set_run_status(ctx.conn, run_id, "approved" if decision == "approved" else "rejected", node="approval")
    events.emit(ctx.conn, run_id, "approval", "started", f"{decision} by {decided_by}")
    cfg = graph_module.thread_config(run_id)
    async with open_graph(ctx, overrides=overrides) as graph:
        if not await graph_module.is_paused(graph, run_id):
            raise RunConflict(f"run {run_id} has no paused graph thread to resume")
        try:
            await graph.ainvoke(Command(resume={"decision": decision, "by": decided_by}), cfg)
        except Exception as exc:
            logger.exception("resuming %s failed", run_id, extra={"run_id": run_id})
            run_store.set_run_status(ctx.conn, run_id, "failed", error=f"{type(exc).__name__}: {exc}")
    return run_store.get_run(ctx.conn, run_id)["status"]
