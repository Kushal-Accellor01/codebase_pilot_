"""GitHub writes for Step 8 (C3): branch, commits through the contents API, pull request, issue, labels.

Everything is idempotent: an open PR for the branch is updated instead of duplicated, an open issue with the
run's marker is reused. Needs a token with contents, pull_requests and issues write access on the repo.
"""
import base64
import logging
import re
from dataclasses import dataclass

from app.updates.github import GitHubClient

logger = logging.getLogger(__name__)

LABELS = (("codebase-pilot", "0E8A16"), ("dependencies", "0366D6"))


class GitHubWriteError(RuntimeError):
    pass


@dataclass
class Published:
    kind: str  # pull_request | issue
    repo: str
    number: int | None
    url: str | None
    branch: str | None
    state: str  # open | updated


def branch_name(package: str, target: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{package}-{target}").strip("-.")
    return f"pilot/{safe}"


def _check(status: int, body: object, what: str, ok: tuple[int, ...] = (200, 201)) -> object:
    if status not in ok:
        message = body.get("message") if isinstance(body, dict) else body
        raise GitHubWriteError(f"{what} failed (HTTP {status}): {message}")
    return body


async def ensure_labels(client: GitHubClient, repo: str) -> None:
    for name, color in LABELS:
        status, _ = await client.request_json("POST", f"repos/{repo}/labels", {"name": name, "color": color})
        if status not in (200, 201, 422):  # 422: already exists
            logger.warning("could not create label %s (HTTP %s)", name, status)


async def _commit_files(client: GitHubClient, repo: str, branch: str, files: dict[str, bytes], message: str) -> None:
    for path, content in sorted(files.items()):
        status, body = await client.request_json("GET", f"repos/{repo}/contents/{path}?ref={branch}")
        sha = body.get("sha") if status == 200 and isinstance(body, dict) else None
        payload = {"message": message, "content": base64.b64encode(content).decode("ascii"), "branch": branch}
        if sha:
            payload["sha"] = sha
        status, body = await client.request_json("PUT", f"repos/{repo}/contents/{path}", payload)
        _check(status, body, f"committing {path}")


async def _delete_files(client: GitHubClient, repo: str, branch: str, paths: list[str], message: str) -> None:
    """Remove files the fix deleted. A file that is already gone on the branch (a re-run) is fine."""
    for path in sorted(paths):
        status, body = await client.request_json("GET", f"repos/{repo}/contents/{path}?ref={branch}")
        if status == 404:
            continue
        sha = _check(status, body, f"reading {path}")["sha"]  # type: ignore[index]
        status, out = await client.request_json(
            "DELETE", f"repos/{repo}/contents/{path}", {"message": message, "sha": sha, "branch": branch}
        )
        _check(status, out, f"deleting {path}")


async def open_or_update_pr(
    client: GitHubClient, *, repo: str, base: str, branch: str, files: dict[str, bytes], title: str, body: str,
    commit_message: str, deleted: list[str] | None = None,
) -> Published:
    owner = repo.split("/")[0]
    status, existing = await client.request_json("GET", f"repos/{repo}/pulls?head={owner}:{branch}&state=open")
    open_pr = existing[0] if status == 200 and isinstance(existing, list) and existing else None

    status, ref = await client.request_json("GET", f"repos/{repo}/git/ref/heads/{base}")
    base_sha = _check(status, ref, f"reading {base}")["object"]["sha"]  # type: ignore[index]
    status, created = await client.request_json(
        "POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha}
    )
    if status not in (200, 201, 422):  # 422: the branch already exists (a re-run)
        _check(status, created, f"creating branch {branch}")

    await _commit_files(client, repo, branch, files, commit_message)
    await _delete_files(client, repo, branch, deleted or [], commit_message)

    if open_pr:
        status, pr = await client.request_json(
            "PATCH", f"repos/{repo}/pulls/{open_pr['number']}", {"title": title, "body": body}
        )
        pr = _check(status, pr, "updating the pull request")
        state = "updated"
    else:
        status, pr = await client.request_json(
            "POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body}
        )
        pr = _check(status, pr, "opening the pull request")
        state = "open"
    await ensure_labels(client, repo)
    status, _ = await client.request_json(
        "POST", f"repos/{repo}/issues/{pr['number']}/labels", {"labels": [name for name, _ in LABELS]}  # type: ignore[index]
    )
    return Published("pull_request", repo, pr["number"], pr["html_url"], branch, state)  # type: ignore[index]


async def open_issue(client: GitHubClient, *, repo: str, title: str, body: str, marker: str) -> Published:
    """Reuse an open Pilot issue that carries `marker`; otherwise open one."""
    status, found = await client.request_json(
        "GET", f"repos/{repo}/issues?state=open&labels={LABELS[0][0]}&per_page=100"
    )
    if status == 200 and isinstance(found, list):
        for issue in found:
            if marker in (issue.get("body") or "") and "pull_request" not in issue:
                status, updated = await client.request_json(
                    "PATCH", f"repos/{repo}/issues/{issue['number']}", {"title": title, "body": body}
                )
                updated = _check(status, updated, "updating the issue")
                return Published("issue", repo, updated["number"], updated["html_url"], None, "updated")  # type: ignore[index]
    await ensure_labels(client, repo)
    status, issue = await client.request_json(
        "POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": [LABELS[0][0]]}
    )
    issue = _check(status, issue, "opening the issue")
    return Published("issue", repo, issue["number"], issue["html_url"], None, "open")  # type: ignore[index]
