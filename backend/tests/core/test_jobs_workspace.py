import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import db as core_db
from app.core import events, jobs, workspace
from tests.updates.conftest import make_db


@pytest.fixture
def conn(tmp_path):
    c = core_db.connect(str(make_db(tmp_path / "t.db", patched=True)))
    core_db.migrate(c)
    return c


# --- jobs -----------------------------------------------------------------------------------------------


def test_enqueue_claim_complete(conn):
    jid = jobs.enqueue(conn, "scan", {"repo_url": "x"})
    job = jobs.claim(conn, "w1")
    assert job.id == jid and job.kind == "scan" and job.payload == {"repo_url": "x"} and job.attempts == 1
    assert jobs.claim(conn, "w2") is None  # nothing else runnable
    jobs.complete(conn, jid)
    assert jobs.counts(conn) == {"succeeded": 1}


def test_dedupe_key_returns_the_existing_job(conn):
    a = jobs.enqueue(conn, "upgrade_run", {}, dedupe_key="k")
    b = jobs.enqueue(conn, "upgrade_run", {"other": 1}, dedupe_key="k")
    assert a == b and conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_claim_is_fifo_and_respects_run_after(conn):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    jobs.enqueue(conn, "a", {}, run_after=later)
    first = jobs.enqueue(conn, "b", {})
    jobs.enqueue(conn, "c", {})
    assert jobs.claim(conn, "w").id == first
    assert jobs.runnable_count(conn) == 1


def test_retry_with_backoff_then_fail(conn):
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    jid = jobs.enqueue(conn, "k", {}, max_attempts=2, run_after=now - timedelta(seconds=1))
    job = jobs.claim(conn, "w", now=now)
    assert jobs.fail(conn, job, "boom", now=now) == "queued"
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert row["run_after"] == "2026-10-28T12:01:00Z" and row["error"] == "boom"  # 30 s * 2^1
    assert jobs.claim(conn, "w", now=now) is None  # backoff not over yet
    job = jobs.claim(conn, "w", now=now + timedelta(minutes=2))
    assert job.attempts == 2
    assert jobs.fail(conn, job, "boom again", now=now) == "failed"
    assert jobs.counts(conn) == {"failed": 1}


def test_crash_recovery_requeues_stuck_jobs(conn):
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    jid = jobs.enqueue(conn, "k", {}, run_after=old)
    jobs.claim(conn, "dead-worker", now=old)
    assert jobs.recover_stuck(conn) == 1
    assert jobs.claim(conn, "w").id == jid


def test_recent_running_job_is_not_recovered(conn):
    jobs.enqueue(conn, "k", {})
    jobs.claim(conn, "w")
    assert jobs.recover_stuck(conn) == 0


# --- events ---------------------------------------------------------------------------------------------


def test_events_roundtrip_and_after_id(conn):
    a = events.emit(conn, "r1", "prepare", "started", "begin")
    b = events.emit(conn, "r1", "prepare", "finished", "done", {"files": 3}, 12)
    events.emit(conn, "r2", "x", "started")
    got = events.list_events(conn, "r1")
    assert [e.id for e in got] == [a, b] and got[1].data == {"files": 3} and got[1].duration_ms == 12
    assert [e.id for e in events.list_events(conn, "r1", after_id=a)] == [b]
    assert got[0].ts.endswith("Z")


# --- workspace ---------------------------------------------------------------------------------------------


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_captures_output_and_exit_code(tmp_path):
    ok = workspace.run(py("print('hello')"), tmp_path, timeout_s=20)
    assert ok.passed and "hello" in ok.output_tail and ok.exit_code == 0
    bad = workspace.run(py("import sys; print('x'); sys.exit(3)"), tmp_path, timeout_s=20)
    assert not bad.passed and bad.exit_code == 3


def test_command_is_killed_at_timeout(tmp_path):
    result = workspace.run(py("import time; time.sleep(30)"), tmp_path, timeout_s=1)
    assert result.timed_out and result.exit_code is None and not result.passed
    assert result.duration_ms < 15000


def test_shell_strings_are_refused(tmp_path):
    with pytest.raises(TypeError):
        workspace.run("echo hi", tmp_path, timeout_s=5)  # type: ignore[arg-type]


def test_tokens_never_reach_the_child_environment(tmp_path, monkeypatch):
    for name in ("GITHUB_TOKEN", "AZURE_OPENAI_API_KEY", "NPM_REGISTRY_TOKEN"):
        monkeypatch.setenv(name, "super-secret-value-123456")
    code = "import os; print(sorted(k for k in os.environ if 'TOKEN' in k or 'KEY' in k)); print(os.environ.get('CI'))"
    result = workspace.run(py(code), tmp_path, timeout_s=20)
    assert "[]" in result.output_tail and "super-secret" not in result.output_tail
    assert "true" in result.output_tail


def test_output_is_redacted_and_tail_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    code = (
        "print('\\n'.join(f'line {i}' for i in range(500)));"
        "print('token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked');"
        "print('Authorization: Bearer abc.def.ghi')"
    )
    result = workspace.run(py(code), tmp_path, timeout_s=20)
    lines = result.output_tail.splitlines()
    assert len(lines) == workspace.OUTPUT_TAIL_LINES and "line 0" not in result.output_tail
    assert "ghp_" not in result.output_tail and "abc.def.ghi" not in result.output_tail


@pytest.mark.parametrize(
    "url,slug",
    [("https://github.com/acme/shop", "acme/shop"), ("https://github.com/acme/shop.git", "acme/shop"),
     ("git@github.com:acme/shop.git", "acme/shop"), ("/local/path", None), (None, None)],
)
def test_repo_slug(url, slug):
    assert workspace.repo_slug(url) == slug


def test_allowlist_refuses_other_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "acme/shop")
    workspace.assert_exec_allowed("acme/shop")
    for repo in ("acme/other", None):
        with pytest.raises(workspace.ExecNotAllowed):
            workspace.assert_exec_allowed(repo)
    with pytest.raises(workspace.ExecNotAllowed):
        workspace.npm_test(tmp_path, "evil/repo")
    with pytest.raises(workspace.ExecNotAllowed):
        workspace.npm_ci(tmp_path, "evil/repo")


def test_npm_install_rejects_odd_package_specs(tmp_path, monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "acme/shop")
    for bad in ("express; rm -rf /", "../evil", "a b"):
        with pytest.raises(ValueError):
            workspace.npm_install_exact(tmp_path, "acme/shop", bad, "1.0.0")


def test_parse_test_counts():
    assert workspace.parse_test_counts("  9 passing (113ms)") == (9, 9)
    assert workspace.parse_test_counts("  1 passing (91ms)\n  8 failing\n") == (1, 9)
    assert workspace.parse_test_counts("  3 failing") == (0, 3)
    assert workspace.parse_test_counts("no summary") == (None, None)


def test_worktree_create_reuse_diff_remove(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        assert workspace.git(clone, *args).passed
    (clone / "a.txt").write_text("one\n", encoding="utf-8")
    assert workspace.git(clone, "add", "-A").passed
    assert workspace.git(clone, "commit", "-q", "-m", "init").passed
    sha = workspace.git(clone, "rev-parse", "HEAD").output_tail.strip()

    dest = tmp_path / "runs" / "r1"
    assert workspace.create_worktree(clone, sha, dest) == dest
    assert workspace.create_worktree(clone, sha, dest) == dest  # idempotent
    (dest / "a.txt").write_text("two\n", encoding="utf-8")
    patch = workspace.diff(dest)
    assert "-one" in patch and "+two" in patch
    workspace.remove_worktree(clone, dest)
    assert not Path(dest).exists()
