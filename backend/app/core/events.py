"""run_events: the live trace of an upgrade run and its audit log. All SQL for them lives here."""
import json
import sqlite3
from dataclasses import dataclass


@dataclass
class RunEvent:
    id: int
    run_id: str
    node: str
    status: str  # started | finished | waiting | error
    summary: str | None
    data: dict | None
    duration_ms: int | None
    ts: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def emit(
    conn: sqlite3.Connection, run_id: str, node: str, status: str, summary: str | None = None,
    data: dict | None = None, duration_ms: int | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO run_events (run_id, node, status, summary, data, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, node, status, summary, json.dumps(data) if data is not None else None, duration_ms),
        )
    return cur.lastrowid


def list_events(conn: sqlite3.Connection, run_id: str, after_id: int = 0, limit: int = 500) -> list[RunEvent]:
    rows = conn.execute(
        "SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?", (run_id, after_id, limit)
    ).fetchall()
    return [
        RunEvent(r["id"], r["run_id"], r["node"], r["status"], r["summary"],
                 json.loads(r["data"]) if r["data"] else None, r["duration_ms"], r["ts"])
        for r in rows
    ]
