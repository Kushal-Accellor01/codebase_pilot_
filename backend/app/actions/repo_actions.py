"""G1/G3/G4 wiring: what Pilot does for a repository after a scan, besides the upgrade runs.

* a comment on every open Dependabot / Renovate pull request (BOT_PR_MODE)
* the dependency dashboard issue and one issue per owning team

Only for GitHub repositories that Pilot may act on: a token is set and the repository is in EXEC_ALLOWED_REPOS,
the same gate the pull-request path has. Everything here is idempotent (comments and issues are found by marker).
"""
import logging
import re
import sqlite3

from app.actions import bot_prs, issues
from app.core import workspace
from app.core.config import Settings
from app.orchestrator import store as run_store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)


def eligible_repo(conn: sqlite3.Connection, scan_id: str, settings: Settings) -> tuple[str | None, str]:
    """(GitHub slug, "") when Pilot may write to the scanned repository, else (None, reason)."""
    if not settings.github_token:
        return None, "no GITHUB_TOKEN"
    repo = workspace.repo_identity(run_store.scan_repo(conn, scan_id)["repo_url"])
    if not (repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repo)):
        return None, f"{repo or 'the repository'} is not a GitHub repository"
    try:
        workspace.assert_exec_allowed(repo)
    except workspace.ExecNotAllowed as exc:
        return None, str(exc)
    return repo, ""


async def run_repo_actions(
    conn: sqlite3.Connection, scan_id: str, settings: Settings, http: HttpClient | None, *,
    bot_comments: bool = True, issue_pages: bool = True,
) -> dict:
    """Run both actions; one failing does not stop the other. Raises at the end so the job is retried."""
    repo, reason = eligible_repo(conn, scan_id, settings)
    if repo is None or http is None:
        return {"skipped": reason or "no HTTP client"}
    result: dict = {"repo": repo}
    errors: list[str] = []
    if bot_comments and settings.bot_pr_mode != "off":
        try:
            authors = [a.strip() for a in settings.bot_authors.split(",") if a.strip()]
            result["bot_prs"] = await bot_prs.enrich_bot_prs(
                conn, GitHubClient(http), repo=repo, mode=settings.bot_pr_mode, bot_authors=authors
            )
        except Exception as exc:
            logger.exception("bot PR enrichment failed", extra={"scan_id": scan_id})
            errors.append(f"bot PRs: {type(exc).__name__}: {exc}")
    if issue_pages:
        try:
            result["issues"] = await issues.publish_issues(conn, scan_id, http)
        except Exception as exc:
            logger.exception("publishing issues failed", extra={"scan_id": scan_id})
            errors.append(f"issues: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return result
