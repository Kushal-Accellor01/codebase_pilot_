"""Git worktrees and the only way repository code is executed (BS 7.6, CLAUDE.md).

Running a repository's install and tests runs its code, so:
  * only repos listed in EXEC_ALLOWED_REPOS may be executed;
  * argument lists only (shell=False), a stripped environment (no tokens), a timeout that kills the
    whole process tree, and output that is tail-limited and redacted;
  * npm installs always use --ignore-scripts.
"""
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

INSTALL_TIMEOUT_S = 120
TEST_TIMEOUT_S = 180
GIT_TIMEOUT_S = 60
OUTPUT_TAIL_LINES = 200
MAX_OUTPUT_CHARS = 2_000_000

# Environment variables a child process may see. Anything else (tokens, API keys) is dropped.
_ENV_ALLOW = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR", "APPDATA",
    "LOCALAPPDATA", "PROGRAMFILES", "ProgramFiles", "COMSPEC", "ComSpec", "PATHEXT", "LANG", "LC_ALL",
)
_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"),
]
_SECRET_ENV_NAMES = ("GITHUB_TOKEN", "AZURE_OPENAI_API_KEY", "NPM_REGISTRY_TOKEN", "NPM_TOKEN")


class ExecNotAllowed(PermissionError):
    """The repository is not in EXEC_ALLOWED_REPOS."""


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    duration_ms: int
    output_tail: str
    timed_out: bool = False
    output: str = ""  # everything the command printed (capped), for parsers; use output_tail for display

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def repo_slug(repo_url: str | None) -> str | None:
    """'https://github.com/org/repo(.git)' -> 'org/repo'."""
    if not repo_url:
        return None
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+?)(?:\.git)?/?$", repo_url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_identity(repo_url: str | None) -> str | None:
    """'org/repo' for GitHub URLs; the normalised path itself for local repositories (tests, demos)."""
    if not repo_url:
        return None
    return repo_slug(repo_url) or repo_url.replace("\\", "/").rstrip("/")


def assert_exec_allowed(repo: str | None) -> None:
    allowed = {a.replace("\\", "/").rstrip("/") for a in get_settings().exec_allowed}
    if not repo or repo not in allowed:
        raise ExecNotAllowed(f"{repo or 'unknown repository'} is not in EXEC_ALLOWED_REPOS; static analysis only")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env.update({"NO_COLOR": "1", "CI": "true"})
    env.update(extra or {})
    return env


def redact(text: str) -> str:
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[redacted]", text)
    return text


def tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, subprocess.SubprocessError, OSError):
        proc.kill()


def run(
    args: list[str], cwd: str | Path, *, timeout_s: float, env_extra: dict[str, str] | None = None
) -> CommandResult:
    """Run one command safely. Never raises on a non-zero exit or a timeout; inspect the result."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise TypeError("args must be a list of strings (no shell strings)")
    exe = shutil.which(args[0], path=child_env().get("PATH")) or args[0]
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    started = time.perf_counter()
    proc = subprocess.Popen(
        [exe, *args[1:]], cwd=str(cwd), env=child_env(env_extra), shell=False,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            output = ""
    return CommandResult(
        command=" ".join(args),
        exit_code=None if timed_out else proc.returncode,
        duration_ms=int((time.perf_counter() - started) * 1000),
        output_tail=redact(tail(output or "")),
        timed_out=timed_out,
        output=redact((output or "")[:MAX_OUTPUT_CHARS]),
    )


# --- git ----------------------------------------------------------------------------------------------------------


def git(repo: str | Path, *args: str, timeout_s: float = GIT_TIMEOUT_S) -> CommandResult:
    return run(["git", "-C", str(repo), *args], cwd=repo, timeout_s=timeout_s)


def create_worktree(clone: str | Path, commit: str, dest: str | Path) -> Path:
    """Detached worktree of `commit`; reuses it if it already exists (the prepare step is idempotent)."""
    dest = Path(dest)
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = git(clone, "worktree", "add", "--detach", str(dest), commit)
    if not result.passed:
        raise RuntimeError(f"git worktree add failed: {result.output_tail}")
    return dest


def remove_worktree(clone: str | Path, dest: str | Path) -> None:
    git(clone, "worktree", "remove", "--force", str(dest))
    if Path(dest).exists():
        shutil.rmtree(dest, ignore_errors=True)
    git(clone, "worktree", "prune")


def diff(worktree: str | Path) -> str:
    """The patch git sees in the worktree (the LLM never writes diffs)."""
    result = run(
        ["git", "-C", str(worktree), "diff", "--no-color", "HEAD", "--", ".", ":(exclude)package-lock.json"],
        cwd=worktree, timeout_s=GIT_TIMEOUT_S,
    )
    return result.output if result.passed else ""


# --- npm ------------------------------------------------------------------------------------------------------------


def npm_ci(worktree: str | Path, repo: str | None) -> CommandResult:
    """`npm ci --ignore-scripts` (falls back to `npm install` when there is no lockfile)."""
    assert_exec_allowed(repo)
    has_lock = (Path(worktree) / "package-lock.json").exists()
    cmd = ["npm", "ci" if has_lock else "install", "--ignore-scripts", "--no-audit", "--no-fund"]
    return run(cmd, worktree, timeout_s=INSTALL_TIMEOUT_S)


def npm_install_exact(worktree: str | Path, repo: str | None, package: str, version: str) -> CommandResult:
    """Bump one dependency: `npm install <pkg>@<version> --save-exact --ignore-scripts`."""
    assert_exec_allowed(repo)
    spec = f"{package}@{version}"
    if not re.fullmatch(r"(@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*@[0-9A-Za-z.+-]+", spec):
        raise ValueError(f"refusing to install an unexpected package spec: {spec!r}")
    return run(["npm", "install", spec, "--save-exact", "--ignore-scripts", "--no-audit", "--no-fund"],
               worktree, timeout_s=INSTALL_TIMEOUT_S)


def npm_test(worktree: str | Path, repo: str | None) -> CommandResult:
    assert_exec_allowed(repo)
    return run(["npm", "test"], worktree, timeout_s=TEST_TIMEOUT_S)


_PASSING = re.compile(r"(\d+)\s+passing")
_FAILING = re.compile(r"(\d+)\s+failing")


def parse_test_counts(output: str) -> tuple[int | None, int | None]:
    """(passed, total) from mocha's 'N passing' / 'M failing' lines; (None, None) if neither is present."""
    passing, failing = _PASSING.search(output), _FAILING.search(output)
    if not passing and not failing:
        return None, None
    p = int(passing.group(1)) if passing else 0
    f = int(failing.group(1)) if failing else 0
    return p, p + f
