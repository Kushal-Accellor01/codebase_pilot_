"""A small SQL job queue: enqueue, claim, complete, retry with backoff, crash recovery (BS 7.10).

The interface is what production keeps when the table is swapped for Azure Service Bus.
"""
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BACKOFF_BASE_S = 30
STUCK_AFTER_S = 15 * 60
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(moment: datetime) -> str:
    return moment.strftime(_FMT)


@dataclass
class Job:
    id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


def enqueue(
    conn: sqlite3.Connection, kind: str, payload: dict, *, dedupe_key: str | None = None,
    run_after: datetime | None = None, max_attempts: int = 3,
) -> str:
    """Add a job. With a dedupe_key an existing job is returned instead of adding a second one."""
    job_id = str(uuid.uuid4())
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs (id, kind, payload, dedupe_key, run_after, max_attempts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, kind, json.dumps(payload), dedupe_key, _ts(run_after or _now()), max_attempts),
        )
    if cur.rowcount == 0 and dedupe_key:
        return conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()["id"]
    return job_id


def claim(conn: sqlite3.Connection, worker: str, *, now: datetime | None = None) -> Job | None:
    """Atomically take the oldest runnable job."""
    stamp = _ts(now or _now())
    with conn:
        row = conn.execute(
            """UPDATE jobs
                  SET status = 'running', locked_by = ?, attempts = attempts + 1, locked_at = ?
                WHERE id = (SELECT id FROM jobs
                             WHERE status = 'queued' AND run_after <= ?
                             ORDER BY created_at, rowid LIMIT 1)
                  AND status = 'queued'
            RETURNING id, kind, payload, attempts, max_attempts""",
            (worker, stamp, stamp),
        ).fetchone()
    if row is None:
        return None
    return Job(row["id"], row["kind"], json.loads(row["payload"]), row["attempts"], row["max_attempts"])


def complete(conn: sqlite3.Connection, job_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'succeeded', finished_at = ?, error = NULL WHERE id = ?",
            (_ts(_now()), job_id),
        )


def fail(conn: sqlite3.Connection, job: Job, error: str, *, now: datetime | None = None) -> str:
    """Retry with exponential backoff while attempts remain, otherwise mark failed. Returns the new status."""
    moment = now or _now()
    if job.attempts < job.max_attempts:
        run_after = moment + timedelta(seconds=BACKOFF_BASE_S * 2**job.attempts)
        with conn:
            conn.execute(
                "UPDATE jobs SET status = 'queued', run_after = ?, error = ?, locked_by = NULL WHERE id = ?",
                (_ts(run_after), error[:2000], job.id),
            )
        return "queued"
    with conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (error[:2000], _ts(moment), job.id),
        )
    return "failed"


def recover_stuck(conn: sqlite3.Connection, *, older_than_s: int = STUCK_AFTER_S, now: datetime | None = None) -> int:
    """Jobs left 'running' by a crashed worker go back to the queue."""
    cutoff = _ts((now or _now()) - timedelta(seconds=older_than_s))
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', locked_by = NULL WHERE status = 'running' AND locked_at < ?",
            (cutoff,),
        )
    return cur.rowcount


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status")}


def runnable_count(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Queued jobs that could be claimed right now (used by `--until-idle`)."""
    return conn.execute(
        "SELECT count(*) FROM jobs WHERE status = 'queued' AND run_after <= ?", (_ts(now or _now()),)
    ).fetchone()[0]
