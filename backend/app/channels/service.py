"""S6: what the Channels page buttons do, on the server: test a connection, install the webhook, publish the report.

Secrets are read here by name (`connectors.base.secret`); results carry a sentence, never a secret.
"""
import re
import sqlite3

from app.changes.sources import dependabot
from app.channels import store as channel_store
from app.connectors import confluence as confluence_connector
from app.connectors import email as email_connector
from app.connectors import github_hooks
from app.connectors import slack as slack_connector
from app.connectors.base import TestResult, secret
from app.core.config import Settings
from app.updates.github import GitHubClient
from app.updates.http import HttpClient

SLUG = re.compile(r"[\w.-]+/[\w.-]+")


def slug_of(repo_id: str) -> str | None:
    return repo_id if SLUG.fullmatch(repo_id) else None


async def test_channel(
    conn: sqlite3.Connection, http: HttpClient, settings: Settings, repo_id: str, kind: str
) -> TestResult:
    channel = channel_store.get_channel(conn, repo_id, kind) or {"config": {}, "secret_ref": None}
    config = channel["config"]
    if kind == "slack":
        result = await slack_connector.test_connection(
            http, token=secret(channel["secret_ref"] or "SLACK_BOT_TOKEN"), webhook_url=secret("SLACK_WEBHOOK_URL"),
            channel=config.get("channel"))
    elif kind == "email":
        result = await email_connector.test_connection(
            host=settings.smtp_host, port=settings.smtp_port, user=settings.smtp_user, password=secret("SMTP_PASSWORD"))
        if result.ok and not (config.get("recipients") or "").strip():
            result = TestResult(False, "Signed in, but no recipients are set")
    elif kind == "confluence":
        result = await confluence_connector.test_connection(
            http, base_url=settings.confluence_base_url, email=settings.confluence_email,
            token=secret("CONFLUENCE_API_TOKEN"), space_id=config.get("space_id"))
    elif kind == "github_webhook":
        slug = slug_of(repo_id)
        if not slug or not settings.github_token:
            result = TestResult(False, "A GitHub repository and GITHUB_TOKEN are needed")
        else:
            result = await github_hooks.ping(GitHubClient(http), slug, settings.pilot_public_url)
    elif kind == "dependabot":
        slug = slug_of(repo_id)
        if not slug or not settings.github_token:
            result = TestResult(False, "A GitHub repository and GITHUB_TOKEN are needed")
        else:
            try:
                alerts = await dependabot.fetch_alerts(GitHubClient(http), slug)
                result = TestResult(True, f"Alerts can be read: {len(alerts)} open")
            except dependabot.AlertsUnavailable as exc:
                result = TestResult(False, str(exc))
    elif kind == "schema_registry":
        if not settings.schema_registry_url:
            result = TestResult(False, "SCHEMA_REGISTRY_URL is not set on the server")
        else:
            resp = await http.get("schema_registry", settings.schema_registry_url.rstrip("/") + "/subjects")
            result = TestResult(resp.status_code == 200, "Schema registry answered" if resp.status_code == 200
                                else f"Schema registry returned {resp.status_code}")
    else:
        result = TestResult(True, "Nothing to connect: this uses the server's GitHub token")
    channel_store.set_status(
        conn, repo_id, kind, "connected" if result.ok else "error", None if result.ok else result.detail)
    return result


async def install_webhook(conn: sqlite3.Connection, http: HttpClient, settings: Settings, repo_id: str) -> TestResult:
    slug = slug_of(repo_id)
    if not slug or not settings.github_token:
        result = TestResult(False, "A GitHub repository and GITHUB_TOKEN are needed to install a webhook")
    else:
        result = await github_hooks.install(
            GitHubClient(http), slug, settings.pilot_public_url, secret("GITHUB_WEBHOOK_SECRET"))
    channel_store.set_status(conn, repo_id, "github_webhook", "connected" if result.ok else "error",
                             None if result.ok else result.detail)
    return result


async def publish_confluence(
    conn: sqlite3.Connection, http: HttpClient, settings: Settings, repo_id: str, scan_id: str
) -> TestResult:
    """Publish (create, then update) the repository report as one page. Only when a person presses Publish."""
    from app.api import queries

    channel = channel_store.get_channel(conn, repo_id, "confluence")
    config = (channel or {}).get("config") or {}
    token = secret("CONFLUENCE_API_TOKEN")
    if not (settings.confluence_base_url and settings.confluence_email and token and config.get("space_id")):
        return TestResult(False, "Confluence needs CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, the API token and a space id")
    rows = queries.list_upgrades(conn, scan_id=scan_id)
    counts = queries.list_vulnerabilities(conn, scan_id)["counts"]
    dashboard = (settings.pilot_public_url or "").rstrip("/") + "/updates" if settings.pilot_public_url else None
    html = confluence_connector.report_html(repo_id, rows, counts, dashboard)
    page_id, action = await confluence_connector.publish_page(
        http, base_url=settings.confluence_base_url, email=settings.confluence_email, token=token,
        space_id=config["space_id"], title=f"Dependency report: {repo_id}", html=html, page_id=config.get("page_id"),
        parent_id=config.get("parent_id"))
    channel_store.save_channel(conn, repo_id, "confluence", enabled=True, config={**config, "page_id": page_id},
                               secret_ref=(channel or {}).get("secret_ref"))
    channel_store.set_status(conn, repo_id, "confluence", "connected", None)
    return TestResult(True, f"Report page {action}")
