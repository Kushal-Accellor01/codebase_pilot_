"""FastAPI app (D1 + D2): dashboard queries, decisions, SSE trace, feedback, source overrides.

    uvicorn app.api.main:app --reload --port 8000

PILOT_EMBEDDED_WORKER=1 runs the job worker inside this process (the demo setup); otherwise start
`python -m app.orchestrator.cli worker` separately.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.api import queries, schemas
from app.core import db as core_db
from app.core import events, jobs, preflight, workspace
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database import GraphDatabase
from app.orchestrator import schedule
from app.orchestrator.worker import Services, run_worker
from app.scan import pipeline as scan_pipeline
from app.scan import store as scan_store
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
    state: dict = {"stop": None, "task": None, "rescan": None}

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
        if settings.pilot_embedded_worker and settings.pilot_rescan_minutes > 0:
            state["stop"] = state["stop"] or asyncio.Event()
            state["rescan"] = asyncio.create_task(schedule.rescan_loop(db, settings, state["stop"]))
        yield
        if state["rescan"]:
            state["stop"].set()
            await asyncio.wait_for(state["rescan"], timeout=30)
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

    def writer(authorization: str | None = Header(default=None), x_pilot_user: str | None = Header(default=None)):
        """Who is writing. Without PILOT_API_TOKEN the API is open (localhost demo) and the caller's name is trusted;
        with it, the bearer token is required and the approver is the X-Pilot-User header."""
        if not settings.pilot_api_token:
            return None
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied.encode(), settings.pilot_api_token.encode()):
            raise HTTPException(401, "a valid Authorization: Bearer token is required",
                                headers={"WWW-Authenticate": "Bearer"})
        return (x_pilot_user or "").strip()[:80] or None

    @app.post("/api/scans", response_model=schemas.JobAccepted, status_code=202)
    def start_scan(body: schemas.ScanRequest, conn=Depends(get_conn), _who=Depends(writer)):
        # The API clones whatever it is given, so it only accepts repositories Pilot is configured to work on.
        allowed = {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
        if workspace.repo_identity(body.repo_url) not in allowed:
            raise HTTPException(403, "this repository is not in EXEC_ALLOWED_REPOS; ask an administrator to add it")
        scope = body.scope.model_dump() if body.scope else None
        request_id = scan_store.create_scan_request(
            conn, repo_url=body.repo_url, branch=body.branch, scope=scope or scan_pipeline.full_scope(), only=body.only)
        job_id = jobs.enqueue(
            conn, "scan",
            {"repo_url": body.repo_url, "only": body.only, "branch": body.branch, "scope": scope,
             "request_id": request_id},
        )
        scan_store.set_request_job(conn, request_id, job_id)
        return {"job_id": job_id, "scan_request_id": request_id}

    @app.post("/api/scans/check", response_model=schemas.RepoCheck)
    def check_repository(body: schemas.RepoCheckRequest, _who=Depends(writer)):
        """Can the server read this repository, and may Pilot work on it? Runs `git ls-remote` on the server."""
        url = body.repo_url.strip()
        identity = workspace.repo_identity(url) or url
        allowed = identity in {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
        host = "github" if "github.com" in url else "bitbucket" if "bitbucket.org" in url else (
            "gitlab" if "gitlab" in url else "local" if "://" not in url and not url.startswith("git@") else "other")
        label = queries._repo_label(url)
        if not allowed:
            return {"provider": host, "repo": label, "allowed": False, "ok": False,
                    "detail": "This repository is not in EXEC_ALLOWED_REPOS, so Pilot will not scan it."}
        if url.startswith("-"):
            raise HTTPException(422, "not a repository link")
        found = preflight.check_repo_access(url)[0]
        branches = [b.strip() for b in found.detail.split("e.g.")[-1].split(",")] if found.status == "ok" else []
        return {"provider": host, "repo": label, "allowed": True, "ok": found.status == "ok", "detail": found.detail,
                "branches": branches}

    @app.get("/api/scan-requests/{request_id}", response_model=schemas.ScanRequestOut)
    def scan_request(request_id: str, conn=Depends(get_conn)):
        try:
            return queries.scan_request_detail(conn, request_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.post("/api/scan-requests/{request_id}/cancel", response_model=schemas.ScanRequestOut)
    def cancel_scan(request_id: str, conn=Depends(get_conn), _who=Depends(writer)):
        try:
            queries.cancel_scan_request(conn, request_id)
            return queries.scan_request_detail(conn, request_id)
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.post("/api/upgrades/{run_id}/decision", response_model=schemas.DecisionAccepted, status_code=202)
    def decide(run_id: str, body: schemas.DecisionRequest, conn=Depends(get_conn), who=Depends(writer)):
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
            {"run_id": run_id, "decision": body.decision, "decided_by": who or body.decided_by,
             "comment": body.comment},
            dedupe_key=f"resume_upgrade:{run_id}:{body.decision}",
        )
        return {"status": "queued"}

    @app.post("/api/feedback", response_model=schemas.FeedbackSaved, status_code=201)
    def feedback(body: schemas.FeedbackRequest, conn=Depends(get_conn), who=Depends(writer)):
        try:
            return {
                "id": queries.save_feedback(
                    conn,
                    target_kind=body.target_kind,
                    target_id=body.target_id,
                    verdict=body.verdict,
                    user_name=who or body.user_name or settings.pilot_demo_user,
                    comment=body.comment,
                )
            }
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.post("/api/sources/override", status_code=201)
    def source_override(body: schemas.SourceOverride, conn=Depends(get_conn), _who=Depends(writer)):
        sources.save_override(
            sources.overrides_path(), body.ecosystem, body.name, body.owner, body.repo, body.directory
        )
        updates_store.delete_package_source(conn, body.ecosystem, body.name)
        return {"saved": f"{body.ecosystem}/{body.name}", "next": "re-run the dependency update step to use it"}

    @app.post("/api/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ):
        """A push to the default branch of an allowed repository queues a scan (Step 8). Signed with
        GITHUB_WEBHOOK_SECRET; without a secret the endpoint does not exist."""
        secret = settings.github_webhook_secret
        if not secret:
            raise HTTPException(404, "webhook is not enabled")
        raw = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        if x_github_event == "ping":
            return {"status": "pong"}
        if x_github_event != "push":
            return {"status": "ignored", "reason": f"event {x_github_event}"}
        try:
            payload = json.loads(raw)
            repo = payload["repository"]
            slug = repo["full_name"]
            branch = payload["ref"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, "not a push payload") from exc
        if branch != f"refs/heads/{repo.get('default_branch', 'main')}":
            return {"status": "ignored", "reason": f"push to {branch}, not the default branch"}
        if slug not in {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}:
            return {"status": "ignored", "reason": f"{slug} is not in EXEC_ALLOWED_REPOS"}
        conn = core_db.connect(db)
        try:
            job = jobs.enqueue(
                # html_url (no .git) is how people type the repository in the dashboard; ingest also normalises.
                conn, "scan", {"repo_url": repo.get("html_url") or f"https://github.com/{slug}", "only": None},
                dedupe_key=f"scan:{slug}:{payload.get('after', '')}",
            )
        finally:
            conn.close()
        return {"status": "queued", "job_id": job}

    # --- scan results (S3) ------------------------------------------------------------------------------------

    @app.get("/api/scans/{scan}/summary", response_model=schemas.ScanSummary)
    def scan_summary(scan: str, conn=Depends(get_conn)):
        try:
            return queries.scan_summary(conn, queries.resolve_scan(conn, scan))
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/scans/{scan}/vulnerabilities", response_model=schemas.VulnerabilityList)
    def scan_vulnerabilities(scan: str, conn=Depends(get_conn)):
        try:
            return queries.list_vulnerabilities(conn, queries.resolve_scan(conn, scan))
        except queries.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/api/vulnerabilities", response_model=schemas.VulnerabilityList)
    def all_vulnerabilities(conn=Depends(get_conn)):
        return queries.list_vulnerabilities_all(conn)

    @app.get("/api/scans/{scan}/sbom")
    def scan_sbom(scan: str, conn=Depends(get_conn)):
        try:
            scan_id = queries.resolve_scan(conn, scan)
        except queries.NotFound as exc:
            raise not_found(exc) from exc
        path = Path(settings.sbom_dir) / f"{scan_id}.cdx.json"
        if not path.is_file():
            raise HTTPException(404, "no bill of materials for this scan: it was not selected, or there is no lockfile")
        return FileResponse(path, media_type="application/vnd.cyclonedx+json", filename=f"{scan_id}.cdx.json")

    # --- server-sent events ----------------------------------------------------------------------------------

    @app.get("/api/scan-requests/{request_id}/events")
    async def scan_request_events(
        request_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None),
        after: int | None = Query(default=None),
    ):
        probe = core_db.connect(db)
        try:
            queries.scan_request_detail(probe, request_id)
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
                    batch = scan_store.list_scan_events(conn, request_id, cursor)
                    for event in batch:
                        cursor = event["id"]
                        yield {"id": str(event["id"]), "event": "scan_event", "data": json.dumps(
                            {"stage": event["stage"], "status": event["status"], "summary": event["summary"],
                             "ts": event["ts"]})}
                    current = scan_store.get_scan_request(conn, request_id)
                    if not batch and current and current["status"] in ("complete", "failed", "cancelled"):
                        yield {"event": "end", "data": json.dumps({"status": current["status"]})}
                        return
                    await asyncio.sleep(SSE_POLL_S)
            finally:
                conn.close()

        return EventSourceResponse(stream())


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
