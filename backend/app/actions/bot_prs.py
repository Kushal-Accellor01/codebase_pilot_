"""G1: enrich Dependabot / Renovate pull requests with Pilot's findings (Trigger B).

BOT_PR_MODE
  off                 nothing
  comment_only        one comment per bot PR, edited in place: impact, test results, link to Pilot's PR
  comment_and_commit  also push the verified edits to the bot's branch, but only for an approved run.
                      Bots stop rebasing a branch someone else pushed to; the comment says so.

What changed is read from the manifests at the PR's base and head commits, never from the PR title.
"""

import base64
import json
import re
import sqlite3

from app.actions import github as gh_writes
from app.core import events
from app.orchestrator import store as run_store
from app.updates.github import GitHubClient
from app.updates.versions import clean_version

MARKER = "<!-- codebase-pilot:bot-enrichment -->"
SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def manifest_changes(base: dict, head: dict) -> list[tuple[str, str | None, str]]:
    """(package, old constraint, new constraint) for every dependency the PR adds or changes."""
    changes = []
    for section in SECTIONS:
        old, new = base.get(section, {}) or {}, head.get(section, {}) or {}
        for name, constraint in new.items():
            if old.get(name) != constraint:
                changes.append((name, old.get(name), constraint))
    return changes


def version_of(constraint: str) -> str | None:
    """'^5.1.0' / '~5.1' / '5.1.0' -> '5.1.0'-ish; None when there is no version in it."""
    m = re.search(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", constraint or "")
    if m:
        return clean_version(m.group(0))
    m = re.search(r"(\d+)(?:\.(\d+))?", constraint or "")
    return f"{m.group(1)}.{m.group(2) or 0}.0" if m else None


def find_run(conn: sqlite3.Connection, package: str, new_constraint: str):
    """The newest Pilot run for `package` whose target is in the same major as the PR's new version."""
    wanted = (version_of(new_constraint) or "").split(".")[0]
    for run in reversed(run_store.list_runs(conn)):
        if run["package"] == package and run["to_version"] and run["to_version"].split(".")[0] == wanted:
            return run
    return None


def render_comment(conn: sqlite3.Connection, run, committed: list[str], mode: str) -> str:
    from app.api import queries

    detail = queries.get_upgrade(conn, run["id"])
    evidence, tests = detail["evidence"], detail["tests"]
    findings = [f for f in detail["findings"] if f["confirmed"] is not False]
    pr = detail["pull_request"]
    lines = [MARKER, "", f"### Codebase Pilot: {run['package']} {run['from_version']} → {run['to_version']}", ""]
    lines.append(
        f"Risk **{detail['run']['risk']}**. {len(findings)} lines in {len({f['file_path'] for f in findings})} files "
        f"use APIs that change; {evidence['runtime_warnings']} already warn at runtime, "
        f"{evidence['production_lines']} matching log lines in production."
    )
    lines += ["", "| File | Line | API | Found by |", "| --- | --- | --- | --- |"]
    for f in findings:
        lines.append(f"| `{f['file_path']}` | {f['line']} | `{f['symbol'] or '-'}` | {', '.join(f['sources'])} |")
    unpatched, patched = tests.get("unpatched") or {}, tests.get("patched") or {}
    if unpatched:
        lines += [
            "",
            f"Tests on the new version without code changes: "
            f"{'pass' if unpatched.get('passed') else 'fail'} "
            f"({unpatched.get('tests_passed')} of {unpatched.get('tests_total')}).",
        ]
    if patched:
        lines.append(
            f"With Pilot's fix: {'pass' if patched.get('passed') else 'fail'} "
            f"({patched.get('tests_passed')} of {patched.get('tests_total')})."
        )
    if pr and (pr.get("url") or "").startswith("http"):
        lines += ["", f"Pilot's own pull request with the fix: {pr['url']}"]
    patch = (detail["patches"] or [{}])[-1]
    if patch.get("diff"):
        lines += [
            "",
            "<details><summary>Verified fix (diff)</summary>",
            "",
            "```diff",
            patch["diff"][:20000],
            "```",
            "",
            "</details>",
        ]
    if committed:
        lines += [
            "",
            f"Pilot pushed the verified edits to this branch ({', '.join(f'`{c}`' for c in committed)}). "
            "Dependabot and Renovate stop rebasing a branch that someone else pushed to.",
        ]
    elif mode == "comment_and_commit":
        lines += ["", "Pilot pushes the fix to this branch only after the run is approved in the dashboard."]
    return "\n".join(lines) + "\n"


async def _read_manifest(client: GitHubClient, repo: str, ref: str) -> dict:
    text = await client.get_text(f"repos/{repo}/contents/package.json", params={"ref": ref})
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


async def _upsert_comment(client: GitHubClient, repo: str, number: int, body: str) -> str:
    status, comments = await client.request_json("GET", f"repos/{repo}/issues/{number}/comments?per_page=100")
    existing = next((c for c in (comments or []) if MARKER in (c.get("body") or "")), None) if status == 200 else None
    if existing:
        status, out = await client.request_json(
            "PATCH", f"repos/{repo}/issues/comments/{existing['id']}", {"body": body}
        )
        gh_writes._check(status, out, "updating the comment")
        return "updated"
    status, out = await client.request_json("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
    gh_writes._check(status, out, "posting the comment")
    return "created"


async def _commit_edits(client: GitHubClient, repo: str, branch: str, edits: list[dict], message: str) -> list[str]:
    """Apply each stored edit to the file on the bot's branch; only edits that still match exactly once."""
    done: list[str] = []
    by_file: dict[str, list[dict]] = {}
    for edit in edits:
        by_file.setdefault(edit["file"], []).append(edit)
    for path, file_edits in by_file.items():
        status, body = await client.request_json("GET", f"repos/{repo}/contents/{path}?ref={branch}")
        if status != 200 or not isinstance(body, dict):
            continue
        text = base64.b64decode(body["content"]).decode("utf-8")
        for edit in file_edits:
            if text.count(edit["find"]) == 1:
                text = text.replace(edit["find"], edit["replace"], 1)
        if text == base64.b64decode(body["content"]).decode("utf-8"):
            continue
        status, out = await client.request_json(
            "PUT",
            f"repos/{repo}/contents/{path}",
            {
                "message": message,
                "branch": branch,
                "sha": body["sha"],
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            },
        )
        gh_writes._check(status, out, f"committing {path}")
        done.append(path)
    return done


async def enrich_bot_prs(
    conn: sqlite3.Connection, client: GitHubClient, *, repo: str, mode: str, bot_authors: list[str]
) -> list[dict]:
    if mode == "off":
        return []
    status, prs = await client.request_json("GET", f"repos/{repo}/pulls?state=open&per_page=50")
    if status != 200 or not isinstance(prs, list):
        return []
    results = []
    for pr in prs:
        if (pr.get("user") or {}).get("login") not in bot_authors:
            continue
        base, head = (
            await _read_manifest(client, repo, pr["base"]["sha"]),
            await _read_manifest(client, repo, pr["head"]["sha"]),
        )
        for package, _old, new in manifest_changes(base, head):
            run = find_run(conn, package, new)
            if run is None:
                results.append({"pr": pr["number"], "package": package, "action": "no_run"})
                continue
            committed: list[str] = []
            if mode == "comment_and_commit" and run["status"] in ("approved", "pr_opened"):
                edits = (run_store.load_patches(conn, run["id"]) or [{}])[-1].get("edits", [])
                committed = await _commit_edits(
                    client, repo, pr["head"]["ref"], edits, f"Fix {package} upgrade (Codebase Pilot)"
                )
            action = await _upsert_comment(client, repo, pr["number"], render_comment(conn, run, committed, mode))
            events.emit(
                conn,
                run["id"],
                "bot_pr",
                "finished",
                f"{action} comment on PR #{pr['number']}",
                {"pr": pr["number"], "committed": committed},
            )
            results.append(
                {"pr": pr["number"], "package": package, "action": action, "committed": committed, "run_id": run["id"]}
            )
    return results
