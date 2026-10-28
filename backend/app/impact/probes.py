"""Runtime evidence: depd deprecation warnings on the current version and failures on the target version.

Both probes run the repository's own tests (allowlisted repos only, --ignore-scripts, stripped environment).
"""
import re
from dataclasses import dataclass
from pathlib import Path

from app.core import workspace
from app.core.contracts import ProbeResult

# depd prints: "<timestamp> <pkg> deprecated <message> at <file>:<line>:<col>"; the file may be absolute,
# relative, POSIX or Windows (with a drive letter).
DEPD_RE = re.compile(
    r"(?P<pkg>@?[\w./-]+) deprecated (?P<msg>.+?)\s+(?:at\s+)?"
    r"(?P<file>(?:[A-Za-z]:)?[^\s:]+(?:\.[cm]?[jt]sx?)):(?P<line>\d+):(?P<col>\d+)\s*$"
)
FRAME_RE = re.compile(r"^\s*at .*?\(?(?P<file>(?:[A-Za-z]:)?[^\s():]+\.[cm]?[jt]sx?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")


@dataclass
class Deprecation:
    package: str
    message: str
    file_path: str
    line: int
    column: int

    @property
    def symbol(self) -> str | None:
        """'req.param(name): Use ...' -> 'req.param'."""
        m = re.match(r"([\w$]+(?:\.[\w$]+)+)", self.message)
        return m.group(1) if m else None


def _relative(path: str, repo_root: str) -> str:
    path = path.replace("\\", "/")
    root = repo_root.rstrip("/\\").replace("\\", "/") + "/"
    if path.lower().startswith(root.lower()):
        path = path[len(root):]
    return path


def parse_deprecations(output: str, repo_root: str) -> list[Deprecation]:
    out, seen = [], set()
    for raw in output.splitlines():
        m = DEPD_RE.search(raw.strip())
        if not m:
            continue
        path = _relative(m["file"], repo_root)
        if "node_modules/" in path:
            continue
        key = (m["pkg"], m["msg"], path, int(m["line"]))
        if key not in seen:
            seen.add(key)
            out.append(Deprecation(m["pkg"], m["msg"], path, int(m["line"]), int(m["col"])))
    return out


@dataclass
class Frame:
    file_path: str
    line: int


def parse_failure_frames(output: str, repo_root: str) -> list[Frame]:
    """Where errors were thrown in repository code, from stack traces in a failing run.

    Per stack trace only the first (innermost) frame in repository code counts: that is the line that
    broke. Callers further down the stack are not affected themselves. Test files and node_modules are
    skipped, since a test only shows where an assertion ran.
    """
    frames, seen = [], set()
    taken = False  # already took the innermost repository frame of the current trace
    for raw in output.splitlines():
        m = FRAME_RE.match(raw)
        if not m:
            taken = False  # a new trace begins after any non-frame line
            continue
        if taken:
            continue
        path = _relative(m["file"], repo_root)
        if (
            path.startswith("node:") or "node_modules/" in path or re.match(r"[A-Za-z]:", path)
            or path.startswith("/") or "/test/" in f"/{path}" or ".test." in path or ".spec." in path
        ):
            continue
        taken = True
        key = (path, int(m["line"]))
        if key not in seen:
            seen.add(key)
            frames.append(Frame(path, int(m["line"])))
    return frames


def to_probe(result: workspace.CommandResult) -> ProbeResult:
    passed, total = workspace.parse_test_counts(result.output)
    return ProbeResult(
        command=result.command, exit_code=result.exit_code, passed=result.passed, duration_ms=result.duration_ms,
        output_tail=result.output_tail, timed_out=result.timed_out, tests_passed=passed, tests_total=total,
    )


@dataclass
class ProbeOutcome:
    probe: ProbeResult
    output: str
    install_ok: bool = True
    install_tail: str = ""


def run_baseline(worktree: str | Path, repo: str | None) -> ProbeOutcome:
    """Install and run the tests on the current version; the output carries the depd warnings."""
    install = workspace.npm_ci(worktree, repo)
    if not install.passed:
        return ProbeOutcome(to_probe(install), install.output, False, install.output_tail)
    result = workspace.npm_test(worktree, repo)
    return ProbeOutcome(to_probe(result), result.output)


def run_upgrade_probe(worktree: str | Path, repo: str | None, package: str, target: str) -> ProbeOutcome:
    """Bump only the package (no code changes) and run the tests: what breaks without a fix."""
    install = workspace.npm_install_exact(worktree, repo, package, target)
    if not install.passed:
        return ProbeOutcome(to_probe(install), install.output, False, install.output_tail)
    result = workspace.npm_test(worktree, repo)
    return ProbeOutcome(to_probe(result), result.output)
