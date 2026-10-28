"""G3/G4: tracking issues. One dashboard issue per repository (like Renovate's), and one issue per owning team.

Both are found again by a hidden marker and updated in place, never duplicated. Without a GitHub token the issue
body is written to REPORTS_DIR instead.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.actions import github as gh_writes
from app.actions.codeowners import load_codeowners, team_for
from app.api import queries
from app.core.config import get_settings
from app.orchestrator import store as run_store
from app.updates import store as updates_store
from app.updates.github import GitHubClient
from app.updates.http import HttpClient

DASHBOARD_MARKER = "<!-- codebase-pilot:dashboard -->"


def team_marker(team: str) -> str:
    return f"<!-- codebase-pilot:team={team} -->"


@dataclass
class Item:
    package: str
    current: str | None
    target: str | None
    risk: str
    reachability: str
    status: str
    pr_url: str | None
    lines: int
    files: list[str]


def collect(conn: sqlite3.Connection, scan_id: str) -> list[Item]:
    runs = {
        r["dependency_update_id"]: r for r in conn.execute("SELECT * FROM upgrade_runs WHERE scan_id = ?", (scan_id,))
    }
    reachable = queries._reachable_updates(conn, scan_id)
    items = []
    for row in updates_store.list_dependency_updates(conn, scan_id):
        if row["status"] in ("up_to_date", "unsupported_ecosystem"):
            continue
        run = runs.get(row["id"])
        findings = [f for f in run_store.load_findings(conn, run["id"], include_rejected=False)] if run else []
        pr = run_store.load_pull_request(conn, run["id"]) if run else None
        items.append(
            Item(
                package=row["name"],
                current=row["current_version"],
                target=row["recommended_target"],
                risk=queries.effective_risk(row, run),
                reachability=queries.reachability(row, reachable),
                status=queries.RUN_LABELS[run["status"]][0] if run else row["status"].replace("_", " "),
                pr_url=pr["url"] if pr and (pr["url"] or "").startswith("http") else None,
                lines=len(findings),
                files=sorted({f.file_path for f in findings}),
            )
        )
    order = {k: i for i, k in enumerate(queries.RISKS)}
    return sorted(items, key=lambda i: (order.get(i.risk, 9), i.package))


def render_dashboard(items: list[Item], repo: str, scan_date: str | None) -> str:
    lines = [DASHBOARD_MARKER, "", f"# Dependency dashboard: {repo}", ""]
    lines.append(
        f"Last scan: {scan_date or 'unknown'}. {len(items)} dependencies need attention. "
        "This issue is updated by Codebase Pilot; edit nothing here."
    )
    lines += [
        "",
        "| Risk | Package | Version | Reachability | Status | Affected | Link |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i in items:
        move = f"{i.current} → {i.target}" if i.target and i.target != i.current else (i.current or "-")
        link = f"[pull request]({i.pr_url})" if i.pr_url else "-"
        affected = f"{i.lines} lines in {len(i.files)} files" if i.lines else "-"
        lines.append(
            f"| {i.risk} | {i.package} | {move} | {i.reachability.replace('_', ' ')} | {i.status} | {affected} | {link} |"
        )
    return "\n".join(lines) + "\n"


def render_team(team: str, items: list[Item], repo: str) -> str:
    lines = [team_marker(team), "", f"# Upgrades that touch code owned by {team} ({repo})", ""]
    for i in items:
        lines.append(f"## {i.package} {i.current} → {i.target} ({i.risk})")
        lines.append(f"Status: {i.status}. {i.lines} affected lines.")
        lines += [f"- `{f}`" for f in i.files]
        if i.pr_url:
            lines.append(f"- [Pull request]({i.pr_url})")
        lines.append("")
    return "\n".join(lines)


def group_by_team(items: list[Item], rules) -> dict[str, list[Item]]:
    teams: dict[str, list[Item]] = {}
    for item in items:
        if not item.files:
            continue
        team = team_for(item.files, rules)
        if team:
            teams.setdefault(team, []).append(item)
    return teams


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


async def _publish(
    conn, http: HttpClient | None, repo: str | None, title: str, body: str, marker: str, local_name: str
) -> dict:
    settings = get_settings()
    if settings.github_token and http is not None and repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        published = await gh_writes.open_issue(GitHubClient(http), repo=repo, title=title, body=body, marker=marker)
        return {"url": published.url, "state": published.state, "where": "github"}
    reports = Path(settings.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{local_name}.md"
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
    return {"url": str(path.resolve()), "state": "local", "where": "file"}


async def publish_issues(
    conn: sqlite3.Connection,
    scan_id: str,
    http: HttpClient | None = None,
    *,
    dashboard: bool = True,
    teams: bool = True,
) -> list[dict]:
    source = run_store.scan_repo(conn, scan_id)
    repo = source["repo_url"] or ""
    from app.core.workspace import repo_identity

    identity = repo_identity(repo)
    label = queries._repo_label(repo)
    items = collect(conn, scan_id)
    info = updates_store.get_scan_info(conn, scan_id)
    out: list[dict] = []
    if dashboard and items:
        body = render_dashboard(items, label, info["scanned_at"])
        out.append(
            {
                "kind": "dashboard",
                **await _publish(
                    conn,
                    http,
                    identity,
                    f"Dependency dashboard ({label})",
                    body,
                    DASHBOARD_MARKER,
                    f"issue_dashboard_{_slug(label)}",
                ),
            }
        )
    if teams:
        for team, team_items in sorted(group_by_team(items, load_codeowners(source["clone_path"])).items()):
            body = render_team(team, team_items, label)
            out.append(
                {
                    "kind": "team",
                    "team": team,
                    **await _publish(
                        conn,
                        http,
                        identity,
                        f"Dependency upgrades for {team}",
                        body,
                        team_marker(team),
                        f"issue_team_{_slug(team)}",
                    ),
                }
            )
    return out
