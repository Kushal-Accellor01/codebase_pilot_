"""S6: the only messages Pilot sends without being asked to. They say that something needs a person, never what the
code looks like: package, version, risk, counts and a link to the dashboard.

Two moments: an upgrade waits for a decision, and a pull request (or issue) was opened. Each is sent once per run
(a `notify` event on the run is the marker), to the Slack and email channels the repository has switched on.
"""
import logging
import sqlite3

from app.channels import store as channel_store
from app.connectors import email as email_connector
from app.connectors import slack as slack_connector
from app.connectors.base import secret
from app.core import events
from app.core.config import Settings
from app.orchestrator import store as run_store
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)

KIND_BY_STATUS = {"awaiting_approval": "needs_decision", "pr_opened": "pr_opened", "issue_opened": "pr_opened"}


def repo_id(repo_url: str | None) -> str:
    from app.core.workspace import repo_identity

    return repo_identity(repo_url) or repo_url or ""


def message_for(run, kind: str, findings: int, files: int, public_url: str | None) -> tuple[str, str]:
    """(subject, text) without code: names, versions, risk, counts and a link."""
    move = f"{run['package']} {run['from_version']} to {run['to_version']}"
    risk = run["risk_level"] or "unknown"
    link = f"\n{public_url.rstrip('/')}/upgrades/{run['id']}" if public_url else ""
    if kind == "needs_decision":
        return (f"Upgrade needs a decision: {move}",
                f"{move} needs a decision. Risk {risk}; {findings} lines in {files} files are affected.{link}")
    return (f"Pull request opened: {move}", f"A pull request for {move} was opened and is waiting for review.{link}")


async def after_run(conn: sqlite3.Connection, http: HttpClient | None, settings: Settings, run_id: str) -> list[str]:
    """Notify for the run's current status if that was not done yet. Returns what was sent ('slack', 'email')."""
    run = run_store.get_run(conn, run_id)
    kind = KIND_BY_STATUS.get(run["status"]) if run else None
    if kind is None or http is None:
        return []
    if any(e.node == "notify" and (e.summary or "").startswith(kind) for e in events.list_events(conn, run_id)):
        return []  # already sent for this moment
    source = run_store.scan_repo(conn, run["scan_id"])
    repo_key = repo_id(source["repo_url"])
    channels = channel_store.list_channels(conn, repo_key)
    findings = run_store.load_findings(conn, run_id, include_rejected=False)
    files = len({f.file_path for f in findings})
    subject, text = message_for(run, kind, len(findings), files, settings.pilot_public_url)
    sent: list[str] = []
    attempted = False
    for name in ("slack", "email"):
        channel = channels.get(name)
        if not channel or not channel["enabled"]:
            continue
        attempted = True
        try:
            if name == "slack":
                config = channel["config"]
                result = await slack_connector.post_message(
                    http, text=text, channel=config.get("channel"),
                    token=secret(channel["secret_ref"] or "SLACK_BOT_TOKEN"), webhook_url=secret("SLACK_WEBHOOK_URL"))
            else:
                result = await _email(settings, channel, subject, text)
        except Exception as exc:  # a notification must never fail a run
            logger.exception("notification failed", extra={"run_id": run_id})
            result = None
            channel_store.set_status(conn, repo_key, name, "error", f"{type(exc).__name__}: {exc}"[:200])
        if result is not None:
            if result.ok:
                sent.append(name)
                channel_store.set_status(conn, repo_key, name, "connected", None)
            else:
                channel_store.set_status(conn, repo_key, name, "error", result.detail)
    if attempted:  # the marker keeps a second pass from sending again; no channel on means nothing to mark
        events.emit(conn, run_id, "notify", "finished", f"{kind}: {', '.join(sent) or 'not delivered'}")
    return sent


async def _email(settings: Settings, channel: dict, subject: str, text: str):
    config = channel["config"]
    return await email_connector.send(
        host=settings.smtp_host or "", port=settings.smtp_port, user=settings.smtp_user,
        password=secret("SMTP_PASSWORD"), sender=settings.smtp_from,
        recipients=[r.strip() for r in (config.get("recipients") or "").split(",") if r.strip()],
        subject=subject, body=text)
