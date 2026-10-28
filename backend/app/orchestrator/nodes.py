"""Node logic of the upgrade run (B1). Engine-agnostic: LangGraph (graph.py) and the sequential fallback
(runner.py) call the same async functions. Every node is idempotent and traced into run_events."""
import functools
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core import events, workspace
from app.core.config import Settings
from app.core.contracts import DependencyChangeEvent, PatchResult, ProbeResult, UpgradeState
from app.core.llm import LLM
from app.impact.analyze import analyse_impact
from app.orchestrator import store as run_store
from app.orchestrator.config_loader import RepoPilotConfig, load_repo_config
from app.planning.plan import build_plan, route_after_plan
from app.remediation import fix as fix_module
from app.telemetry.seeded import SeededLogSource, collect_evidence
from app.updates import store as updates_store
from app.updates.events import build_event
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STATE_TAIL_LINES = 40

Publisher = Callable[["RunContext", UpgradeState], Awaitable[dict]]


@dataclass
class RunContext:
    conn: sqlite3.Connection
    http: HttpClient | None
    llm: LLM | None
    settings: Settings
    workspace_root: Path
    publisher: Publisher | None = None  # defaults to app.actions.publish.publish


# --- helpers ------------------------------------------------------------------------------------------------------


def load_event(conn: sqlite3.Connection, run_id: str) -> tuple[DependencyChangeEvent, sqlite3.Row, sqlite3.Row]:
    """(event, run row, dependency_updates row) rebuilt from the database."""
    run = run_store.get_run(conn, run_id)
    if run is None:
        raise KeyError(f"unknown upgrade run {run_id}")
    row = updates_store.get_update_row(conn, run["dependency_update_id"])
    importers = updates_store.importing_files_by_dep(conn, run["scan_id"])
    return build_event(conn, row, importers), run, row


def _short(probe: ProbeResult | None) -> dict | None:
    if probe is None:
        return None
    data = probe.model_dump()
    data["output_tail"] = "\n".join(data["output_tail"].splitlines()[-STATE_TAIL_LINES:])
    return data


def traced(name: str):
    """Write started/finished/error events with timing, track the current node, fail the run on errors."""

    def decorate(fn: Callable[[RunContext, UpgradeState], Awaitable[dict]]):
        @functools.wraps(fn)
        async def wrapper(ctx: RunContext, state: UpgradeState) -> dict:
            run_id = state["run_id"]
            started = time.perf_counter()
            events.emit(ctx.conn, run_id, name, "started")
            with ctx.conn:
                ctx.conn.execute("UPDATE upgrade_runs SET current_node = ? WHERE id = ?", (name, run_id))
            try:
                update = await fn(ctx, state)
            except Exception as exc:
                logger.exception("node %s failed", name, extra={"run_id": run_id, "node": name})
                events.emit(ctx.conn, run_id, name, "error", f"{type(exc).__name__}: {exc}",
                            duration_ms=int((time.perf_counter() - started) * 1000))
                run_store.set_run_status(ctx.conn, run_id, "failed", node=name, error=f"{type(exc).__name__}: {exc}")
                raise
            summary = update.pop("_summary", None)
            events.emit(ctx.conn, run_id, name, "waiting" if update.pop("_waiting", False) else "finished", summary,
                        duration_ms=int((time.perf_counter() - started) * 1000))
            return update

        return wrapper

    return decorate


def _config(state: UpgradeState) -> RepoPilotConfig:
    return RepoPilotConfig(**state.get("config", {}))


def _exec_allowed(repo: str | None) -> bool:
    try:
        workspace.assert_exec_allowed(repo)
        return True
    except workspace.ExecNotAllowed:
        return False


# --- nodes ----------------------------------------------------------------------------------------------------------


@traced("prepare")
async def prepare(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    event, run, _row = load_event(ctx.conn, run_id)
    source = run_store.scan_repo(ctx.conn, run["scan_id"])
    clone = source["clone_path"]
    if not clone or not Path(clone).is_dir():
        raise RuntimeError(f"the scanned code is gone ({clone}); re-scan the repository")
    repo = workspace.repo_identity(source["repo_url"])
    config, warnings = load_repo_config(clone)
    worktree = workspace.create_worktree(clone, source["commit_sha"] or "HEAD", ctx.workspace_root / "runs" / run_id)
    workspace.write_npmrc(worktree)
    run_store.set_run_status(ctx.conn, run_id, "analyzing", node="prepare")
    allowed = _exec_allowed(repo)
    # Warnings ride on the one finished event: a separate event per warning made the step look finished repeatedly.
    summary = f"worktree ready, repository {'may' if allowed else 'may not'} be executed ({event.package})"
    if warnings:
        summary += "; " + "; ".join(warnings)
    return {
        "worktree": str(worktree), "repo": repo, "clone": clone, "config": config.model_dump(), "attempts": 0,
        "exec_allowed": allowed, "route": "full", "_summary": summary,
    }


@traced("impact")
async def impact(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    event, run, row = load_event(ctx.conn, run_id)
    config = _config(state)
    result = await analyse_impact(
        ctx.conn, event=event, dep_node_id=row["dep_node_id"], root=Path(state["worktree"]),
        repo=state.get("repo"), run_id=run_id, llm=ctx.llm, run_probes=state.get("exec_allowed", False),
        test_args=config.test_command,
    )
    run_store.save_findings(ctx.conn, run_id, result.findings)
    live = [f for f in result.findings if f.confirmed is not False]
    return {
        "scope_files": result.scope_files,
        "baseline": _short(result.baseline),
        "upgrade_probe": _short(result.upgrade_probe),
        "findings_count": len(live),
        "_summary": f"{len(live)} affected lines in {len({f.file_path for f in live})} files"
        + (f" ({'; '.join(result.notes)})" if result.notes else ""),
    }


@traced("telemetry")
async def telemetry(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    logs = ctx.settings.seeded_logs_dir
    directory = Path(logs) if logs else Path(state.get("clone", "")) / "logs"
    findings = run_store.load_findings(ctx.conn, run_id, include_rejected=False)
    evidence = []
    if ctx.settings.azure_log_analytics_workspace_id:
        from app.telemetry.azure_monitor import AzureMonitorLogSource

        service = _config(state).service
        if service:
            evidence = collect_evidence(
                AzureMonitorLogSource.from_settings(ctx.settings, service), findings, service=service,
                source_name="azure_monitor",
            )
    elif directory.is_dir() and any(directory.glob("*.jsonl")):
        evidence = collect_evidence(SeededLogSource(directory), findings)
    run_store.save_telemetry(ctx.conn, run_id, evidence)
    total = sum(e.count for e in evidence)
    summary = f"{total} matching production log lines in {len(evidence)} patterns"
    return {"telemetry_count": len(evidence), "_summary": summary}


@traced("plan")
async def plan(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    event, run, _row = load_event(ctx.conn, run_id)
    config = _config(state)
    findings = run_store.load_findings(ctx.conn, run_id)
    evidence = run_store.load_telemetry(ctx.conn, run_id)
    probe = ProbeResult(**state["upgrade_probe"]) if state.get("upgrade_probe") else None
    result = await build_plan(ctx.llm, event, findings, probe, evidence, manifest_changes=True)
    run_store.save_plan(ctx.conn, run_id, result)
    with ctx.conn:
        ctx.conn.execute("UPDATE upgrade_runs SET risk_level = ? WHERE id = ?", (result.severity, run_id))

    route, reason = route_after_plan(result), ""
    if route == "full" and not config.enabled:
        route, reason = "report_only", "Pilot is disabled for this repository"
    elif route == "full" and config.mode == "shadow":
        route, reason = "report_only", "shadow mode: report only"
    elif route == "full" and not config.severity_allows(result.severity):
        route = "report_only"
        reason = f"severity {result.severity} is below the configured minimum {config.min_severity}"
    elif route == "full" and not state.get("exec_allowed"):
        route, reason = "report_only", "the repository is not allowed to run code: no fix was attempted"
    return {"route": route, "severity": result.severity,
            "_summary": f"{result.severity}, about {result.effort_hours} h" + (f"; {reason}" if reason else "")}


@traced("fix")
async def fix(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    event, _run, _row = load_event(ctx.conn, run_id)
    attempts = state.get("attempts", 0) + 1
    worktree = Path(state["worktree"])
    run_store.set_run_status(ctx.conn, run_id, "fixing", node="fix")
    # Every attempt starts from the scanned code: the upgrade probe and earlier attempts changed the worktree.
    workspace.reset_worktree(worktree)
    findings = run_store.load_findings(ctx.conn, run_id, include_rejected=False)
    patch = await fix_module.make_patch(
        ctx.llm, event, worktree, state.get("repo"), findings, state.get("scope_files", []),
        attempt=attempts, failure_tail=state.get("failure_tail"), allowed_paths=_config(state).allowed_paths,
    )
    run_store.save_patch(ctx.conn, run_id, patch)
    return {"attempts": attempts, "patch": patch.model_dump(),
            "_summary": f"attempt {attempts}: {len(patch.edits)} edits applied, {len(patch.rejected)} rejected"}


@traced("validate")
async def validate(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    run_store.set_run_status(ctx.conn, run_id, "validating", node="validate")
    config = _config(state)
    patch = PatchResult(**state["patch"])
    probe = fix_module.validate(Path(state["worktree"]), state.get("repo"), config.test_command)
    patch.validation = probe
    run_store.save_patch(ctx.conn, run_id, patch)
    update: dict = {"patch": patch.model_dump(), "failure_tail": None if probe.passed else probe.output_tail}
    attempts = state.get("attempts", 1)
    if probe.passed:
        update["route"] = "full"
        message = f"tests pass on attempt {attempts} ({probe.tests_passed}/{probe.tests_total})"
    elif attempts >= MAX_ATTEMPTS:
        update["route"] = "needs_engineer"
        message = f"tests still fail after {attempts} attempts; an engineer has to take over"
    else:
        return {**update, "_summary": f"tests fail on attempt {attempts}; trying again"}
    run_store.set_run_status(ctx.conn, run_id, "awaiting_approval", node="validate")
    return {**update, "_summary": message, "_waiting": True}


@traced("act")
async def act(ctx: RunContext, state: UpgradeState) -> dict:
    publisher = ctx.publisher
    if publisher is None:
        from app.actions.publish import publish as publisher
    result = await publisher(ctx, state)
    return {**result, "_summary": result.get("summary", "published")}


@traced("finish")
async def finish(ctx: RunContext, state: UpgradeState) -> dict:
    run_id = state["run_id"]
    run = run_store.get_run(ctx.conn, run_id)
    if state.get("route") == "report_only":
        run_store.set_run_status(ctx.conn, run_id, "report_only", node="finish")
    elif state.get("decision") == "rejected":
        run_store.set_run_status(ctx.conn, run_id, "rejected", node="finish")
    elif run["status"] not in ("pr_opened", "needs_engineer", "issue_opened", "failed", "resolved"):
        run_store.set_run_status(ctx.conn, run_id, run["status"], node="finish")
    clone, worktree = state.get("clone"), state.get("worktree")
    if clone and worktree and run_store.get_run(ctx.conn, run_id)["status"] != "awaiting_approval":
        workspace.remove_worktree(clone, worktree)
    return {"_summary": f"run ended as {run_store.get_run(ctx.conn, run_id)['status']}"}
