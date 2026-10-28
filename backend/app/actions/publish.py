"""Step 8: turn an approved run into a pull request or an issue (C3).

With a GITHUB_TOKEN and a GitHub repository the PR is opened through the REST API. Without either (offline demo,
local mirror) the fix is committed on a branch, pushed back into the origin repository, and the PR body is
written to REPORTS_DIR so nothing about the flow changes. Both paths are idempotent.
"""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from app.actions import github as gh_writes
from app.actions import render
from app.actions.codeowners import load_codeowners, owners_by_file
from app.actions.policy import decide_action
from app.core import workspace
from app.core.config import get_settings
from app.core.contracts import PatchResult, Plan, ProbeResult
from app.orchestrator import store as run_store
from app.orchestrator.config_loader import RepoPilotConfig
from app.updates.github import GitHubClient

logger = logging.getLogger(__name__)

EXCLUDED = ("node_modules/", ".npmrc")


def changed_files(worktree: Path) -> dict[str, bytes]:
    """Files the fix touched (edits, package.json and the lockfile), read from the worktree."""
    status = workspace.run(["git", "-c", "core.quotepath=false", "-C", str(worktree), "status", "--porcelain", "-uall"],
                           worktree, timeout_s=workspace.GIT_TIMEOUT_S)
    files: dict[str, bytes] = {}
    for line in status.output.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if "D" in code or any(path.startswith(p) or path == p.rstrip("/") for p in EXCLUDED):
            continue
        target = worktree / path
        if target.is_file():
            files[path] = target.read_bytes()
    return files


def _diff_stat(worktree: Path) -> str | None:
    result = workspace.run(["git", "-C", str(worktree), "diff", "--stat", "--no-color", "HEAD"], worktree,
                           timeout_s=workspace.GIT_TIMEOUT_S)
    return result.output.strip() or None if result.passed else None


def _parse_author(text: str) -> tuple[str, str]:
    m = re.match(r"\s*(.*?)\s*<(.+?)>\s*$", text)
    return (m.group(1) or "Codebase Pilot", m.group(2)) if m else (text.strip() or "Codebase Pilot", "pilot@localhost")


def push_local(worktree: Path, origin: str, branch: str, message: str) -> None:
    """Commit the worktree on `branch` and push it to the origin repository (a local path; see local_origin)."""
    name, email = _parse_author(get_settings().pilot_git_author)
    steps = [
        ["git", "-C", str(worktree), "checkout", "-B", branch],
        ["git", "-C", str(worktree), "add", "-A"],
        ["git", "-C", str(worktree), "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m",
         message, "--allow-empty"],
        ["git", "-C", str(worktree), "push", "--force", origin, f"HEAD:refs/heads/{branch}"],
    ]
    for step in steps:
        result = workspace.run(step, worktree, timeout_s=workspace.GIT_TIMEOUT_S * 2)
        if not result.passed:
            raise RuntimeError(f"{' '.join(step[3:6])} failed: {result.output_tail[-300:]}")


def local_origin(repo_url: str | None, clone: str | None) -> str:
    """Where the fallback pushes: the scanned repository when it is a local path, else the clone.

    Never a remote URL: without a token that push has no credentials and would hang or fail the run."""
    if repo_url and Path(repo_url).is_dir():
        return repo_url
    if clone and Path(clone).is_dir():
        return clone
    raise RuntimeError("no local repository to push the branch to (no token for GitHub, and the clone is gone)")


def last_failure(patches: list[dict]) -> str | None:
    """Output tail of the newest patch's failed validation, if any."""
    if not patches:
        return None
    validation = patches[-1].get("validation") or {}
    return None if validation.get("passed") else validation.get("output_tail")


def _is_github_slug(repo: str | None) -> bool:
    return bool(repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repo))


def _probe(data: dict | None) -> ProbeResult | None:
    return ProbeResult(**data) if data else None


async def publish(ctx, state) -> dict:
    """The `act` node: policy -> PR / issue -> pull_requests row -> run status."""
    run_id = state["run_id"]
    conn = ctx.conn
    existing = run_store.load_pull_request(conn, run_id)
    if existing is not None:  # idempotent: a second call returns the first result
        return {"pr": dict(existing), "summary": f"already published: {existing['url']}"}

    from app.orchestrator.nodes import load_event

    event, run, _row = load_event(conn, run_id)
    config = RepoPilotConfig(**state.get("config", {}))
    plan: Plan = run_store.load_plan(conn, run_id)
    findings = run_store.load_findings(conn, run_id)
    patches = run_store.load_patches(conn, run_id)
    latest = PatchResult(**{**state["patch"]}) if state.get("patch") else None

    action = decide_action(decision=state.get("decision"), config=config, plan=plan, findings=findings,
                           latest_patch=latest)
    if action == "none":
        return {"summary": "no action: decision, mode or configuration does not allow one"}

    worktree = Path(state["worktree"])
    source = run_store.scan_repo(conn, run["scan_id"])
    repo = state.get("repo")
    owners = owners_by_file(sorted({f.file_path for f in findings}), load_codeowners(state.get("clone")))
    branch = gh_writes.branch_name(event.package, event.target_version or "latest")
    use_github = bool(get_settings().github_token) and _is_github_slug(repo) and ctx.http is not None
    data = render.BodyData(
        run_id=run_id, event=event, plan=plan, findings=findings, telemetry=run_store.load_telemetry(conn, run_id),
        baseline=_probe(state.get("baseline")), unpatched=_probe(state.get("upgrade_probe")),
        patched=latest.validation if latest else None, approver=state.get("decided_by") or "unknown",
        approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        repo_slug=repo if use_github else None, branch=branch, owners=owners, diff_stat=_diff_stat(worktree),
        failure_tail=state.get("failure_tail") or last_failure(patches),
    )
    kind = "pull_request" if action == "pull_request" else "issue"
    body = render.render_body(data, kind=kind)
    title = render.title(event) if kind == "pull_request" else f"Upgrade {event.package} needs an engineer"

    if use_github:
        client = GitHubClient(ctx.http)
        if kind == "pull_request":
            published = await gh_writes.open_or_update_pr(
                client, repo=repo, base=source["branch"] or "main", branch=branch, files=changed_files(worktree),
                title=title, body=body, commit_message=f"Upgrade {event.package} to {event.target_version}",
            )
        else:
            published = await gh_writes.open_issue(client, repo=repo, title=title, body=body,
                                                   marker=render.marker(run_id))
        record = dict(kind=published.kind, repo=repo, number=published.number, url=published.url,
                      branch=published.branch, state=published.state)
    else:
        reports = Path(get_settings().reports_dir)
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"{'pr' if kind == 'pull_request' else 'issue'}_{run_id}.md"
        path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        if kind == "pull_request":
            push_local(worktree, local_origin(source["repo_url"], state.get("clone")), branch,
                       f"Upgrade {event.package} to {event.target_version}")
        record = dict(kind=kind, repo=repo or "local", number=None, url=str(path.resolve()),
                      branch=branch if kind == "pull_request" else None, state="local")

    run_store.save_pull_request(conn, run_id, **record)
    run_store.set_run_status(conn, run_id, "pr_opened" if kind == "pull_request" else "needs_engineer", node="act")
    return {"pr": record, "summary": f"{kind.replace('_', ' ')} {record['url']}"}
