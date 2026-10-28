import sys
from pathlib import Path

import pytest

from app.core import workspace
from tests.conftest import make_sandbox_repo


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_sandbox_repo(tmp_path / "work")


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_reset_removes_edits_and_untracked_files_but_keeps_node_modules(repo):
    (repo / "routes" / "orders.js").write_text("// broken\n", encoding="utf-8")
    (repo / "extra.js").write_text("x", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "keep.txt").write_text("k", encoding="utf-8")
    (repo / ".npmrc").write_text("registry=x\n", encoding="utf-8")
    assert workspace.diff(repo) != ""
    workspace.reset_worktree(repo)
    assert workspace.diff(repo) == ""
    assert "req.param" in (repo / "routes" / "orders.js").read_text(encoding="utf-8")
    assert not (repo / "extra.js").exists()
    assert (repo / "node_modules" / "keep.txt").exists() and (repo / ".npmrc").exists()


def test_no_npmrc_for_the_default_registry(repo, monkeypatch):
    monkeypatch.delenv("NPM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("NPM_REGISTRY_TOKEN", "tok")
    assert workspace.write_npmrc(repo) is False and not (repo / ".npmrc").exists()


def test_custom_registry_writes_npmrc_without_the_token_and_hides_it_from_git(repo, monkeypatch):
    monkeypatch.setenv("NPM_REGISTRY_URL", "https://pkgs.example.com/npm/feed/")
    monkeypatch.setenv("NPM_REGISTRY_TOKEN", "super-secret-token")
    assert workspace.write_npmrc(repo) is True
    text = (repo / ".npmrc").read_text(encoding="utf-8")
    assert "registry=https://pkgs.example.com/npm/feed/" in text
    assert "//pkgs.example.com/npm/feed/:_authToken=${NPM_TOKEN}" in text
    assert "super-secret-token" not in text
    assert ".npmrc" not in workspace.git(repo, "status", "--porcelain").output  # excluded from git
    assert ".npmrc" not in workspace.diff(repo)
    workspace.write_npmrc(repo)  # idempotent
    exclude = Path(workspace.git(repo, "rev-parse", "--git-path", "info/exclude").output.strip())
    exclude = exclude if exclude.is_absolute() else repo / exclude
    assert exclude.read_text(encoding="utf-8").splitlines().count(".npmrc") == 1


def test_custom_registry_without_token_has_no_auth_line(repo, monkeypatch):
    monkeypatch.setenv("NPM_REGISTRY_URL", "https://pkgs.example.com/feed")
    monkeypatch.delenv("NPM_REGISTRY_TOKEN", raising=False)
    workspace.write_npmrc(repo)
    assert "_authToken" not in (repo / ".npmrc").read_text(encoding="utf-8")


def test_token_reaches_install_commands_but_not_tests(repo, monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "acme/shop")
    monkeypatch.setenv("NPM_REGISTRY_TOKEN", "super-secret-token")
    captured: list[tuple[list[str], dict]] = []

    def fake_run(args, cwd, *, timeout_s, env_extra=None):
        captured.append((args, env_extra or {}))
        return workspace.CommandResult(" ".join(args), 0, 1, "")

    monkeypatch.setattr(workspace, "run", fake_run)
    workspace.npm_ci(repo, "acme/shop")
    workspace.npm_install_exact(repo, "acme/shop", "express", "5.1.0")
    workspace.npm_test(repo, "acme/shop")
    workspace.run_tests(repo, "acme/shop", ["npm", "run", "test:ci"])
    assert captured[0][1] == {"NPM_TOKEN": "super-secret-token"}
    assert captured[1][1] == {"NPM_TOKEN": "super-secret-token"}
    assert captured[2][1] == {} and captured[3][1] == {}
    assert captured[3][0] == ["npm", "run", "test:ci"]


def test_run_tests_only_starts_known_runners(repo, monkeypatch):
    monkeypatch.setenv("EXEC_ALLOWED_REPOS", "acme/shop")
    for bad in (["rm", "-rf", "."], ["bash", "-c", "x"], ["curl", "http://x"]):
        with pytest.raises(ValueError):
            workspace.run_tests(repo, "acme/shop", bad)
    with pytest.raises(workspace.ExecNotAllowed):
        workspace.run_tests(repo, "evil/repo", ["npm", "test"])


def test_diff_excludes_lockfile_and_npmrc(repo):
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (repo / "routes" / "orders.js").write_text("// changed\n", encoding="utf-8")
    patch = workspace.diff(repo)
    assert "routes/orders.js" in patch and "package-lock.json" not in patch
