import asyncio
import base64
import json
import subprocess

import pytest
import respx

from app.actions import github as gh_writes
from app.actions import publish, render
from app.core import workspace
from app.core.contracts import DependencyChangeEvent, Finding, Plan, ProbeResult, TelemetryEvidence
from app.updates.github import GitHubClient
from app.updates.http import HttpClient
from app.updates.models import ChangeRef
from tests.conftest import make_sandbox_repo

GH = "https://api.github.com"
REPO = "acme/pilot-sandbox-shop"


def run(coro):
    return asyncio.run(coro)


def probe(passed, p=9, t=9):
    return ProbeResult(command="npm test", exit_code=0 if passed else 1, passed=passed, duration_ms=1,
                       output_tail="", tests_passed=p, tests_total=t)


def body_data(**kw) -> render.BodyData:
    event = DependencyChangeEvent(
        event_id="e", scan_id="s", ecosystem="npm", package="express", dependency_type="runtime",
        manifest_path="package.json", current_version="4.21.2", current_version_basis="lockfile", target_version="5.3.0",
        update_type="major", requires_manifest_change=True, runtime_requirement_change=False, is_deprecated=False,
        importer_count=4, priority_score=70, risk_level="high", status="analyzed", summary="Express 5 removes APIs.",
        changes=[ChangeRef(id=1, kind="removed", breaking=True, api_symbols=["req.param"], description="req.param removed",
                           version="5.0.0", evidence_url="https://github.com/expressjs/express/releases/tag/v5.0.0",
                           evidence_quote="Remove req.param(name) | use req.params")],
    )
    base = dict(
        run_id="run-1", event=event,
        plan=Plan(severity="critical", effort_hours=4.5, rationale="Tests fail without changes.", steps=["Bump.", "Fix."],
                  validation=["Tests pass."]),
        findings=[Finding(file_path="routes/orders.js", line=8, snippet="x", symbol="req.param", change_id=1,
                          sources=["pattern", "runtime_deprecation", "test_failure"], confidence=1.0),
                  Finding(file_path="lib/x.js", line=1, snippet="y", sources=["pattern"], confidence=0.4, confirmed=False)],
        telemetry=[TelemetryEvidence(source="seeded_logs", signature="express deprecated req.param(name)", count=42,
                                     first_seen=None, last_seen="2026-10-28T10:00:00Z", sample=None, related_symbol="req.param")],
        baseline=probe(True), unpatched=probe(False, 1, 9), patched=probe(True),
        approver="alice", approved_at="2026-10-28 12:00 UTC", repo_slug=REPO, branch="pilot/express-5.3.0",
        owners={"routes/orders.js": ["@acme/team-shop"]}, diff_stat=" routes/orders.js | 2 +-",
    )
    return render.BodyData(**{**base, **kw})


# --- render -------------------------------------------------------------------------------------------------------


def test_pr_body_has_every_section_and_escapes_table_cells():
    text = render.render_body(body_data())
    assert text.startswith("<!-- codebase-pilot:run=run-1 -->")
    assert "Express 5 removes APIs." in text and "**Severity:** critical · **Estimated effort:** about 4.5 h" in text
    assert "| req.param removed (`req.param`) | yes | 5.0.0 |" in text
    assert "“Remove req.param(name) \\| use req.params”" in text  # pipes would break the table
    assert "[routes/orders.js:8](https://github.com/acme/pilot-sandbox-shop/blob/pilot/express-5.3.0/routes/orders.js#L8)" in text
    assert "found by code search, runtime warning, failing test) · owner @acme/team-shop" in text
    assert "lib/x.js" not in text  # a finding the model rejected is not listed
    assert "| Current version | passed (9 of 9 tests pass) |" in text
    assert "| New version, no code changes | failed (1 of 9 tests pass) |" in text
    assert "| New version, with this fix | passed (9 of 9 tests pass) |" in text
    assert "42 log lines" in text and "1. Bump." in text and "Changed files" in text
    assert text.rstrip().endswith("Nothing was merged automatically.") and "Approved by alice" in text


def test_body_is_deterministic():
    assert render.render_body(body_data()) == render.render_body(body_data())


def test_local_repositories_get_plain_file_references():
    text = render.render_body(body_data(repo_slug=None))
    assert "`routes/orders.js:8`" in text and "https://github.com/acme" not in text.split("### Tests")[0].split("### Where")[1]


def test_issue_body_explains_and_includes_the_failing_output():
    text = render.render_body(body_data(patched=probe(False, 5, 9), failure_tail="AssertionError: boom"), kind="issue")
    assert "so an engineer needs to take over" in text and "AssertionError: boom" in text
    assert "Changed files" not in text and "Express 5 removes APIs." not in text


def test_title_and_branch_names():
    assert render.title(body_data().event) == "Upgrade express 4.21.2 → 5.3.0 (Codebase Pilot)"
    assert gh_writes.branch_name("express", "5.3.0") == "pilot/express-5.3.0"
    assert gh_writes.branch_name("@scope/pkg", "1.0.0-beta.1") == "pilot/scope-pkg-1.0.0-beta.1"


# --- GitHub writes ---------------------------------------------------------------------------------------------------


async def open_pr(files=None):
    async with HttpClient(wait_scale=0) as http:
        return await gh_writes.open_or_update_pr(
            GitHubClient(http, "tok"), repo=REPO, base="main", branch="pilot/express-5.3.0",
            files=files or {"routes/orders.js": b"new", "package.json": b"{}"}, title="T", body="B", commit_message="msg")


def mock_pr_flow(*, existing_pr=None, branch_exists=False, file_sha=None):
    respx.get(f"{GH}/repos/{REPO}/pulls").respond(200, json=[existing_pr] if existing_pr else [])
    respx.get(f"{GH}/repos/{REPO}/git/ref/heads/main").respond(200, json={"object": {"sha": "base123"}})
    ref = respx.post(f"{GH}/repos/{REPO}/git/refs").respond(
        422 if branch_exists else 201, json={"message": "Reference already exists"} if branch_exists else {})
    respx.get(url__regex=rf"{GH}/repos/{REPO}/contents/.*").respond(200 if file_sha else 404, json={"sha": file_sha} if file_sha else {})
    put = respx.put(url__regex=rf"{GH}/repos/{REPO}/contents/.*").respond(201, json={})
    label_create = respx.post(f"{GH}/repos/{REPO}/labels").respond(422, json={"message": "already_exists"})
    return ref, put, label_create


@respx.mock
def test_new_pull_request_flow():
    ref, put, label_create = mock_pr_flow()
    create = respx.post(f"{GH}/repos/{REPO}/pulls").respond(201, json={"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"})
    labels = respx.post(f"{GH}/repos/{REPO}/issues/7/labels").respond(200, json=[])
    published = run(open_pr())
    assert (published.number, published.state, published.url.endswith("/pull/7")) == (7, "open", True)
    assert json.loads(ref.calls[0].request.content) == {"ref": "refs/heads/pilot/express-5.3.0", "sha": "base123"}
    assert put.call_count == 2
    sent = {c.request.url.path.split("/contents/")[1]: json.loads(c.request.content) for c in put.calls}
    assert base64.b64decode(sent["routes/orders.js"]["content"]) == b"new" and sent["package.json"]["branch"] == "pilot/express-5.3.0"
    assert json.loads(create.calls[0].request.content) == {"title": "T", "head": "pilot/express-5.3.0", "base": "main", "body": "B"}
    assert json.loads(labels.calls[0].request.content) == {"labels": ["codebase-pilot", "dependencies"]}
    assert label_create.call_count == 2  # 422 "already exists" is fine


@respx.mock
def test_existing_open_pr_is_updated_not_duplicated():
    mock_pr_flow(existing_pr={"number": 7, "html_url": "u"}, branch_exists=True, file_sha="oldsha")
    patch = respx.patch(f"{GH}/repos/{REPO}/pulls/7").respond(200, json={"number": 7, "html_url": "https://github.com/x/pull/7"})
    create = respx.post(f"{GH}/repos/{REPO}/pulls").respond(201, json={})
    respx.post(f"{GH}/repos/{REPO}/issues/7/labels").respond(200, json=[])
    published = run(open_pr())
    assert published.state == "updated" and published.number == 7
    assert patch.called and not create.called


@respx.mock
def test_existing_file_sha_is_sent_on_update():
    _, put, _ = mock_pr_flow(branch_exists=True, file_sha="oldsha")
    respx.post(f"{GH}/repos/{REPO}/pulls").respond(201, json={"number": 8, "html_url": "u"})
    respx.post(f"{GH}/repos/{REPO}/issues/8/labels").respond(200, json=[])
    run(open_pr({"a.js": b"x"}))
    assert json.loads(put.calls[0].request.content)["sha"] == "oldsha"


@respx.mock
def test_write_failures_raise_with_the_github_message():
    respx.get(f"{GH}/repos/{REPO}/pulls").respond(200, json=[])
    respx.get(f"{GH}/repos/{REPO}/git/ref/heads/main").respond(404, json={"message": "Not Found"})
    with pytest.raises(gh_writes.GitHubWriteError, match="Not Found"):
        run(open_pr())


@respx.mock
def test_rate_limit_stops_the_flow():
    from app.updates.github import GitHubRateLimited

    respx.get(f"{GH}/repos/{REPO}/pulls").respond(
        200, json=[], headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"})
    with pytest.raises(GitHubRateLimited):
        run(open_pr())


@respx.mock
def test_issue_is_created_once_and_reused_by_marker():
    async def go():
        async with HttpClient(wait_scale=0) as http:
            return await gh_writes.open_issue(GitHubClient(http, "tok"), repo=REPO, title="T", body="B", marker="<!--m-->")

    respx.get(f"{GH}/repos/{REPO}/issues").respond(200, json=[])
    respx.post(f"{GH}/repos/{REPO}/labels").respond(422, json={})
    create = respx.post(f"{GH}/repos/{REPO}/issues").respond(201, json={"number": 3, "html_url": "https://x/3"})
    assert run(go()).state == "open" and create.call_count == 1

    respx.get(f"{GH}/repos/{REPO}/issues").respond(200, json=[
        {"number": 3, "body": "text <!--m-->"}, {"number": 4, "body": "<!--m-->", "pull_request": {}}])
    patch = respx.patch(f"{GH}/repos/{REPO}/issues/3").respond(200, json={"number": 3, "html_url": "https://x/3"})
    again = run(go())
    assert again.state == "updated" and patch.called and create.call_count == 1


# --- local fallback ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def worktree(tmp_path):
    origin = make_sandbox_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    sha = workspace.git(clone, "rev-parse", "HEAD").output.strip()
    wt = workspace.create_worktree(clone, sha, tmp_path / "runs" / "r1")
    return origin, wt


def test_changed_files_include_manifest_and_lockfile_but_not_node_modules_or_npmrc(worktree):
    _, wt = worktree
    (wt / "routes" / "orders.js").write_bytes(b"// fixed\n")
    (wt / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (wt / "node_modules").mkdir(exist_ok=True)
    (wt / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (wt / ".npmrc").write_text("registry=x\n", encoding="utf-8")
    (wt / "new.txt").write_text("n", encoding="utf-8")
    (wt / "data").joinpath("invoice-1.pdf").unlink()
    files = publish.changed_files(wt)
    assert set(files) == {"routes/orders.js", "package-lock.json", "new.txt"}
    assert files["routes/orders.js"] == b"// fixed\n"


def test_local_push_creates_the_branch_in_origin_and_is_repeatable(worktree, monkeypatch):
    origin, wt = worktree
    default_branch = subprocess.run(["git", "-C", str(origin), "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
    monkeypatch.setenv("PILOT_GIT_AUTHOR", "Pilot Bot <bot@example.com>")
    (wt / "routes" / "orders.js").write_text("// fixed\n", encoding="utf-8")
    publish.push_local(wt, str(origin), "pilot/express-5.3.0", "Upgrade express to 5.3.0")
    log = subprocess.run(["git", "-C", str(origin), "log", "-1", "--format=%an <%ae>|%s", "pilot/express-5.3.0"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert log == "Pilot Bot <bot@example.com>|Upgrade express to 5.3.0"
    show = subprocess.run(["git", "-C", str(origin), "show", "pilot/express-5.3.0:routes/orders.js"],
                          capture_output=True, text=True, check=True).stdout
    assert show.startswith("// fixed")
    branches = subprocess.run(["git", "-C", str(origin), "branch", "--list"], capture_output=True, text=True).stdout
    assert "pilot/express-5.3.0" in branches
    publish.push_local(wt, str(origin), "pilot/express-5.3.0", "again")  # a re-run does not fail
    after = subprocess.run(["git", "-C", str(origin), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    assert after.stdout.strip() == default_branch  # the default branch was never touched


def test_last_failure_and_slug_helpers():
    assert publish.last_failure([]) is None
    assert publish.last_failure([{"validation": {"passed": True, "output_tail": "t"}}]) is None
    assert publish.last_failure([{"validation": {"passed": False, "output_tail": "boom"}}]) == "boom"
    assert publish._is_github_slug("acme/shop") and not publish._is_github_slug("C:/a/b") and not publish._is_github_slug(None)
    assert publish._parse_author("A B <a@b.c>") == ("A B", "a@b.c") and publish._parse_author("solo")[1] == "pilot@localhost"
