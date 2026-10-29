"""SQL for change events and the cursors of polling sources (S5, S7)."""
import hashlib
import json
import sqlite3

from app.core.contracts import ChangeEvent


def make_event_id(
    source: str, repo: str, subject_kind: str, subject_name: str, from_version: str | None, to_version: str | None
) -> str:
    """The same change from the same source for the same repository always gets the same id."""
    key = "|".join([source, repo, subject_kind, subject_name, from_version or "", to_version or ""])
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def upsert_event(conn: sqlite3.Connection, event: ChangeEvent, *, run_id: str | None = None) -> bool:
    """Insert, or refresh the payload of a known event. True when the event is new. Status and run link are kept."""
    existing = conn.execute("SELECT run_id FROM change_events WHERE event_id = ?", (event.event_id,)).fetchone()
    with conn:
        if existing is None:
            conn.execute(
                """INSERT INTO change_events (event_id, origin, source, repo, scan_id, subject_kind, subject_name,
                       from_version, to_version, detected_at, payload, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event.event_id, event.origin, event.source, event.repo, event.scan_id, event.subject.kind,
                 event.subject.name, event.from_version, event.to_version, event.detected_at,
                 event.model_dump_json(), run_id),
            )
        else:
            conn.execute(
                "UPDATE change_events SET scan_id = ?, payload = ?, run_id = COALESCE(?, run_id) WHERE event_id = ?",
                (event.scan_id, event.model_dump_json(), run_id, event.event_id),
            )
    return existing is None


def link_run(conn: sqlite3.Connection, event_id: str, run_id: str | None) -> None:
    with conn:
        conn.execute("UPDATE change_events SET run_id = ? WHERE event_id = ?", (run_id, event_id))


def set_status(conn: sqlite3.Connection, event_id: str, status: str, error: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE change_events SET status = ?, error = ? WHERE event_id = ?", (status, error, event_id))


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    return data


def get_event(conn: sqlite3.Connection, event_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM change_events WHERE event_id = ?", (event_id,)).fetchone()
    return _row(row) if row else None


def list_events(
    conn: sqlite3.Connection, *, repo: str | None = None, origin: str | None = None, source: str | None = None,
    status: str | None = None,
) -> list[dict]:
    sql, args = "SELECT * FROM change_events WHERE 1 = 1", []
    for column, value in (("repo", repo), ("origin", origin), ("source", source), ("status", status)):
        if value:
            sql += f" AND {column} = ?"  # noqa: S608 (column names are fixed above)
            args.append(value)
    return [_row(r) for r in conn.execute(sql + " ORDER BY detected_at DESC, rowid DESC", args)]


def events_for_scan(conn: sqlite3.Connection, scan_id: str) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM change_events WHERE scan_id = ?", (scan_id,))]


# --- cursors ----------------------------------------------------------------------------------------------------------


def get_cursor(conn: sqlite3.Connection, source: str, repo: str) -> str | None:
    row = conn.execute("SELECT cursor FROM change_cursors WHERE source = ? AND repo = ?", (source, repo)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, repo: str, cursor: str | None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO change_cursors (source, repo, cursor) VALUES (?, ?, ?)
               ON CONFLICT(source, repo) DO UPDATE SET cursor = excluded.cursor,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
            (source, repo, cursor),
        )
