"""FastAPI app (D1 + D2): dashboard queries, decisions, SSE trace, feedback, source overrides.

    uvicorn app.api.main:app --reload --port 8000

PILOT_EMBEDDED_WORKER=1 runs the job worker inside this process (the demo setup); otherwise start
`python -m app.orchestrator.cli worker` separately.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.api import queries, schemas
from app.core import db as core_db
from app.core import events, jobs, workspace
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database import GraphDatabase
from app.orchestrator.worker import Services, run_worker
from app.updates import sources
from app.updates import store as updates_store

logger = logging.getLogger(__name__)

CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
SSE_POLL_S = 0.5


def create_app(
    db_path: str | None = None, settings: Settings | None = None, services: Services | None = None
) -> FastAPI:
    settings = settings or get_settings()
    db = db_path or settings.codebase_pilot_db
    state: dict = {"stop": None, "task": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging("INFO", None)
        GraphDatabase(db).close()  # creates scans/nodes/edges on a brand-new database
        conn = core_db.connect(db)
        try:
            core_db.migrate(conn)
        finally:
            conn.close()
        if settings.pilot_embedded_worker:
            state["stop"] = asyncio.Event()
            state["task"] = asyncio.create_task(
                run_worker(db, services=services, settings=settings, stop=state["stop"], poll_s=0.5)
            )
        yield
        if state["task"]:
            state["stop"].set()
            await asyncio.wait_for(state["task"], timeout=30)

    app = FastAPI(title="Codebase Pilot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

    def get_conn() -> Iterator:
        # FastAPI may create a dependency in one worker thread and run the endpoint in another; a connection is
        # still used by one request at a time, so cross-thread use is safe here.
        conn = core_db.connect(db, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def not_found(exc: queries.NotFound) -> HTTPException:
        return HTTPException(404, f"not found: {exc}")

    # --- reads -----------------------------------------------------------------------------------------------

    @app.get("/api/health", response_model=schemas.Health)
    def health(conn=Depends(get_conn)):
        return {**queries.health(conn, settings.llm_mode), "embedded_worker": settings.pilot_embedded_worker}

    @app.get("/api/scans", response_model=list[schemas.ScanOut])
    def scans(conn=Depends(get_conn)):
        return queries.list_scans(conn)

    @app.get("/api/scans/{scan}/dependencies", response_model=list[schemas.DependencyRow])
    def dependencies(scan: str, conn=Depends(get_conn)):
        try:
            return queries.list_dependencies(conn, queries.resolve_scan(conn, scan))
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/scans/{scan}/overview", response_model=schemas.Overview, response_model_by_alias=True)
    def overview(scan: str, conn=Depends(get_conn)):
        try:
            return queries.overview(conn, queries.resolve_scan(conn, scan))
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/scans/{scan}/graph", response_model=schemas.Graph)
    def graph(scan: str, run_id: str = Query(...), conn=Depends(get_conn)):
        try:
            return queries.blast_radius(conn, queries.resolve_scan(conn, scan), run_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/dependency-updates/{update_id}")
    def dependency_update(update_id: int, conn=Depends(get_conn)):
        try:
            return queries.get_dependency_event(conn, update_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/upgrades", response_model=list[schemas.UpgradeSummary], response_model_by_alias=True)
    def upgrades(status: str | None = None, scan_id: str | None = None, conn=Depends(get_conn)):
        return queries.list_upgrades(conn, status, scan_id)

    @app.get("/api/upgrades/{run_id}", response_model=schemas.UpgradeDetail, response_model_by_alias=True)
    def upgrade(run_id: str, conn=Depends(get_conn)):
        try:
            return queries.get_upgrade(conn, run_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    # --- writes ----------------------------------------------------------------------------------------------

    @app.post("/api/scans", response_model=schemas.JobAccepted, status_code=202)
    def start_scan(body: schemas.ScanRequest, conn=Depends(get_conn)):
        # The API clones whatever it is given, so it only accepts repositories Pilot is configured to work on.
        allowed = {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
        if workspace.repo_identity(body.repo_url) not in allowed:
            raise HTTPException(403, "this repository is not in EXEC_ALLOWED_REPOS; ask an administrator to add it")
        return {"job_id": jobs.enqueue(conn, "scan", {"repo_url": body.repo_url, "only": body.only})}

    @app.post("/api/upgrades/{run_id}/decision", response_model=schemas.DecisionAccepted, status_code=202)
    def decide(run_id: str, body: schemas.DecisionRequest, conn=Depends(get_conn)):
        try:
            status = queries.run_status(conn, run_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc
        if status != "awaiting_approval":
            raise HTTPException(409, f"run is {status}, not awaiting approval")
        if queries.has_pending_decision(conn, run_id):
            raise HTTPException(409, "a decision for this run is already being processed")
        jobs.enqueue(
            conn,
            "resume_upgrade",
            {"run_id": run_id, "decision": body.decision, "decided_by": body.decided_by, "comment": body.comment},
            dedupe_key=f"resume_upgrade:{run_id}:{body.decision}",
        )
        return {"status": "queued"}

    @app.post("/api/feedback", response_model=schemas.FeedbackSaved, status_code=201)
    def feedback(body: schemas.FeedbackRequest, conn=Depends(get_conn)):
        try:
            return {
                "id": queries.save_feedback(
                    conn,
                    target_kind=body.target_kind,
                    target_id=body.target_id,
                    verdict=body.verdict,
                    user_name=body.user_name or settings.pilot_demo_user,
                    comment=body.comment,
                )
            }
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.post("/api/sources/override", status_code=201)
    def source_override(body: schemas.SourceOverride, conn=Depends(get_conn)):
        sources.save_override(
            sources.overrides_path(), body.ecosystem, body.name, body.owner, body.repo, body.directory
        )
        updates_store.delete_package_source(conn, body.ecosystem, body.name)
        return {"saved": f"{body.ecosystem}/{body.name}", "next": "re-run the dependency update step to use it"}

    # --- server-sent events ----------------------------------------------------------------------------------

    @app.get("/api/upgrades/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None),
        after: int | None = Query(default=None),
    ):
        probe = core_db.connect(db)
        try:
            queries.run_status(probe, run_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc
        finally:
            probe.close()
        cursor = after if after is not None else int(last_event_id) if (last_event_id or "").isdigit() else 0

        async def stream() -> AsyncIterator[dict]:
            nonlocal cursor
            conn = core_db.connect(db)
            try:
                while not await request.is_disconnected():
                    batch = events.list_events(conn, run_id, cursor)
                    for event in batch:
                        cursor = event.id
                        yield {
                            "id": str(event.id),
                            "event": "run_event",
                            "data": json.dumps(
                                {
                                    "run_id": run_id,
                                    "node": event.node,
                                    "status": event.status,
                                    "summary": event.summary,
                                    "ts": event.ts,
                                    "data": event.data,
                                }
                            ),
                        }
                    status = queries.run_status(conn, run_id)
                    caught_up = not batch
                    if caught_up and (status in queries.TERMINAL or status == "awaiting_approval"):
                        yield {"event": "end", "data": json.dumps({"status": status})}
                        return
                    await asyncio.sleep(SSE_POLL_S)
            finally:
                conn.close()

        return EventSourceResponse(stream())

    return app


app = create_app()
