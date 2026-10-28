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
