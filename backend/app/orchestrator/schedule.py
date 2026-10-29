"""Step 8: notice that code changed. Two triggers feed the same `scan` job:

* a GitHub push webhook (api/main.py), and
* this scheduled re-scan of every repository Pilot already knows (PILOT_RESCAN_MINUTES, or the `rescan` CLI command
  from cron / Task Scheduler).

A scan finds the new commit, `dependency_updates` closes runs whose upgrade has landed, and new runs are queued.
Repositories outside EXEC_ALLOWED_REPOS are never re-scanned.
"""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone

from app.channels import store as channel_store
from app.core import db as core_db
from app.core import jobs, workspace
from app.core.config import Settings
from app.orchestrator import store as run_store

logger = logging.getLogger(__name__)


def enqueue_rescans(conn: sqlite3.Connection, settings: Settings, *, now: float | None = None) -> list[str]:
    """One scan job per known, allowed repository. At most one per repository per interval (deduplicated)."""
    interval = max(settings.pilot_rescan_minutes, 1) * 60
    bucket = int((time.time() if now is None else now) // interval)
    allowed = {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
    queued: list[str] = []
    for repo_url in run_store.known_repos(conn):
        if workspace.repo_identity(repo_url) not in allowed:
            continue
        job = jobs.enqueue(conn, "scan", {"repo_url": repo_url, "only": None}, dedupe_key=f"rescan:{repo_url}:{bucket}")
        queued.append(job)
    return queued


async def rescan_loop(db_path: str, settings: Settings, stop: asyncio.Event) -> None:
    """Queue the re-scans every PILOT_RESCAN_MINUTES until `stop` is set."""
    interval = settings.pilot_rescan_minutes * 60
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        conn = core_db.connect(db_path)
        try:
            queued = enqueue_rescans(conn, settings)
            if queued:
                logger.info("queued %d scheduled re-scans", len(queued))
        except Exception:
            logger.exception("scheduled re-scan failed")
        finally:
            conn.close()


# --- S6: the schedules table (what the Channels page edits) ----------------------------------------------------

TICK_SECONDS = 60


def _latest_scan_for(conn: sqlite3.Connection, repo_id: str) -> str | None:
    from app.notifications import repo_id as identity

    for repo_url in run_store.known_repos(conn):
        if identity(repo_url) == repo_id:
            row = conn.execute(
                """SELECT m.scan_id FROM scan_meta m JOIN scans s ON s.id = m.scan_id WHERE m.repo_url = ?
                   ORDER BY s.scanned_at DESC, s.rowid DESC LIMIT 1""", (repo_url,)).fetchone()
            return row["scan_id"] if row else None
    return None


def _repo_url_for(conn: sqlite3.Connection, repo_id: str) -> str | None:
    from app.notifications import repo_id as identity

    return next((u for u in run_store.known_repos(conn) if identity(u) == repo_id), None)


def run_due_schedules(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> list[str]:
    """Queue the job of every schedule that is due and set its next time. Returns the schedule ids that ran.

    rescan -> a scan job; dependabot_alerts -> an alerts job; releases -> the version check again on the latest scan;
    internal_sources has no reader yet, so it only moves its clock.
    Repositories outside EXEC_ALLOWED_REPOS are skipped."""
    now = now or datetime.now(timezone.utc)
    allowed = {a.replace("\\", "/").rstrip("/") for a in settings.exec_allowed}
    ran: list[str] = []
    bucket = int(now.timestamp() // 60)
    for row in channel_store.due_schedules(conn, now):
        repo_id, source = row["repo"], row["source"]
        if repo_id in allowed:
            if source == "rescan":
                url = _repo_url_for(conn, repo_id)
                if url:
                    jobs.enqueue(conn, "scan", {"repo_url": url, "only": None},
                                 dedupe_key=f"sched:rescan:{repo_id}:{bucket}")
            elif source == "dependabot_alerts":
                jobs.enqueue(conn, "dependabot_alerts", {"repo_id": repo_id},
                             dedupe_key=f"sched:alerts:{repo_id}:{bucket}")
            elif source == "releases":
                scan_id = _latest_scan_for(conn, repo_id)
                if scan_id:
                    jobs.enqueue(conn, "dependency_updates", {"scan_id": scan_id, "only": None},
                                 dedupe_key=f"sched:releases:{repo_id}:{bucket}")
        channel_store.mark_ran(conn, row["id"], now)
        ran.append(row["id"])
    return ran


async def schedule_loop(db_path: str, settings: Settings, stop: asyncio.Event) -> None:
    """Check the schedules table every minute until `stop` is set."""
    while not stop.is_set():
        conn = core_db.connect(db_path)
        try:
            if run_due_schedules(conn, settings):
                logger.info("queued jobs for due schedules")
        except Exception:
            logger.exception("schedule tick failed")
        finally:
            conn.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
