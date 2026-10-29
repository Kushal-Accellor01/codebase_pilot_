"""SQL for channels (where Pilot reads changes from and reports to) and schedules (S5, S6).

`config` holds non-secret values only. `secret_ref` is the NAME of a server secret, never its value.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

KINDS = (
    "github_webhook", "dependabot", "bot_prs", "slack", "email", "confluence", "dashboard_issue", "schema_registry",
)
SECRET_PREFIX = "PILOT_SECRET_"
FIXED_SECRET_NAMES = {
    "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "SMTP_PASSWORD",
    "CONFLUENCE_API_TOKEN", "SCHEMA_REGISTRY_AUTH", "BITBUCKET_APP_PASSWORD",
}
SOURCES = ("rescan", "dependabot_alerts", "releases", "internal_sources")
DEFAULT_INTERVALS = {"rescan": 24 * 60, "dependabot_alerts": 30, "releases": 6 * 60, "internal_sources": 15}
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def valid_secret_ref(name: str | None) -> bool:
    """Only allowlisted names may be referenced: the fixed ones and anything under PILOT_SECRET_."""
    return name is None or name in FIXED_SECRET_NAMES or bool(re.fullmatch(rf"{SECRET_PREFIX}[A-Z0-9_]{{1,60}}", name))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- channels ------------------------------------------------------------


def _channel(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["config"] = json.loads(data["config"] or "{}")
    data["enabled"] = bool(data["enabled"])
    return data


def get_channel(conn: sqlite3.Connection, repo: str, kind: str) -> dict | None:
    row = conn.execute("SELECT * FROM channels WHERE repo = ? AND kind = ?", (repo, kind)).fetchone()
    return _channel(row) if row else None


def list_channels(conn: sqlite3.Connection, repo: str) -> dict[str, dict]:
    return {r["kind"]: _channel(r) for r in conn.execute("SELECT * FROM channels WHERE repo = ?", (repo,))}


def save_channel(
    conn: sqlite3.Connection, repo: str, kind: str, *, enabled: bool, config: dict, secret_ref: str | None
) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown channel kind {kind!r}")
    if not valid_secret_ref(secret_ref):
        raise ValueError(f"secret_ref must be one of the known names or start with {SECRET_PREFIX}")
    with conn:
        conn.execute(
            """INSERT INTO channels (repo, kind, enabled, config, secret_ref) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo, kind) DO UPDATE SET enabled = excluded.enabled, config = excluded.config,
                   secret_ref = excluded.secret_ref""",
            (repo, kind, int(enabled), json.dumps(config), secret_ref),
        )


def set_status(conn: sqlite3.Connection, repo: str, kind: str, status: str, error: str | None = None) -> None:
    """Record the result of a connection test or a poll. Creates the row when the channel was never configured."""
    with conn:
        conn.execute(
            """INSERT INTO channels (repo, kind, status, last_checked_at, last_error) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo, kind) DO UPDATE SET status = excluded.status,
                   last_checked_at = excluded.last_checked_at, last_error = excluded.last_error""",
            (repo, kind, status, _now().strftime(_STAMP), error),
        )


# --- schedules ------------------------------------------------------------


def ensure_schedules(conn: sqlite3.Connection, repo: str) -> None:
    """Every repository gets the five default schedules once; existing ones are left alone."""
    for source in SOURCES:
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO schedules (id, source, repo, interval_minutes, enabled, next_run_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (f"{source}:{repo}", source, repo, DEFAULT_INTERVALS[source], 1 if source != "internal_sources" else 0,
                 (_now() + timedelta(minutes=DEFAULT_INTERVALS[source])).strftime(_STAMP)),
            )


def list_schedules(conn: sqlite3.Connection, repo: str | None = None) -> list[dict]:
    sql = "SELECT * FROM schedules" + (" WHERE repo = ?" if repo else "") + " ORDER BY repo, source"
    return [dict(r) | {"enabled": bool(r["enabled"])} for r in conn.execute(sql, (repo,) if repo else ())]


def get_schedule(conn: sqlite3.Connection, schedule_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return dict(row) | {"enabled": bool(row["enabled"])} if row else None


def update_schedule(
    conn: sqlite3.Connection, schedule_id: str, *, interval_minutes: int | None = None, enabled: bool | None = None
) -> bool:
    row = get_schedule(conn, schedule_id)
    if row is None:
        return False
    minutes = interval_minutes if interval_minutes is not None else row["interval_minutes"]
    with conn:
        conn.execute(
            "UPDATE schedules SET interval_minutes = ?, enabled = ?, next_run_at = ? WHERE id = ?",
            (minutes, int(row["enabled"] if enabled is None else enabled),
             (_now() + timedelta(minutes=minutes)).strftime(_STAMP), schedule_id),
        )
    return True


def due_schedules(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    stamp = (now or _now()).strftime(_STAMP)
    rows = conn.execute("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at", (stamp,))
    return [dict(r) for r in rows]


def mark_ran(conn: sqlite3.Connection, schedule_id: str, now: datetime | None = None) -> None:
    now = now or _now()
    row = get_schedule(conn, schedule_id)
    if row is None:
        return
    with conn:
        conn.execute(
            "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
            (now.strftime(_STAMP), (now + timedelta(minutes=row["interval_minutes"])).strftime(_STAMP), schedule_id),
        )
