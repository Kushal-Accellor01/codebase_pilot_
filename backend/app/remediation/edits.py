"""Applying LLM-proposed edits under strict rules (BS 7.5). Violations are recorded, never applied.

Rules, enforced in code:
  * the file is in the package scope or is a test file; the real path stays inside the worktree;
  * the file matches the repository's `allowed_paths` globs (default: everything);
  * package.json and lockfiles are never edited (the manifest bump owns them);
  * `find` occurs exactly once in the current file content;
  * at most 30 edits and 200 changed lines per attempt;
  * after applying, `node --check` on each changed .js file; a syntax error reverts that file's edits.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core import workspace
from app.core.contracts import Edit

logger = logging.getLogger(__name__)

MAX_EDITS = 30
MAX_CHANGED_LINES = 200
PROTECTED_NAMES = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"}
SYNTAX_CHECKED = {".js", ".mjs", ".cjs"}


@dataclass
class ApplyResult:
    applied: list[Edit] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


def _glob_regex(pattern: str) -> re.Pattern:
    """`**` crosses folders, `*` and `?` stay inside one; a trailing `/` means everything below."""
    pattern = pattern.replace("\\", "/").removeprefix("./")
    if pattern.endswith("/"):
        pattern += "**"
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def path_allowed(rel: str, allowed_paths: list[str] | None) -> bool:
    """Does `rel` match one of the globs? None (no restriction configured) allows everything."""
    if allowed_paths is None:
        return True
    rel = rel.replace("\\", "/")
    return any(_glob_regex(p).match(rel) for p in allowed_paths)


def is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return any(p in ("test", "tests", "__tests__", "spec") for p in parts[:-1]) or ".test." in name or ".spec." in name


def _reject(result: ApplyResult, edit: Edit, reason: str) -> None:
    result.rejected.append(
        {"file": edit.file, "find": edit.find[:200], "replace": edit.replace[:200], "reason": reason}
    )


def _resolve(root: Path, rel: str) -> Path | None:
    """The real path of `rel` if it stays inside `root`, else None."""
    if not rel or rel.startswith(("/", "\\")) or ":" in rel:
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _changed_lines(edit: Edit) -> int:
    return len(edit.find.splitlines()) + len(edit.replace.splitlines())


def node_check(path: Path) -> str | None:
    """None when the file parses, else the first lines of node's complaint."""
    result = workspace.run(["node", "--check", str(path)], path.parent, timeout_s=30)
    return None if result.passed else result.output_tail[:400]


def apply_edits(
    root: Path, edits: list[Edit], scope_files: list[str], *, syntax_check: bool = True,
    allowed_paths: list[str] | None = None,
) -> ApplyResult:
    root = Path(root)
    result = ApplyResult()
    scope = {f.replace("\\", "/") for f in scope_files}
    originals: dict[Path, str] = {}
    changed_lines = 0

    for index, edit in enumerate(edits):
        rel = edit.file.replace("\\", "/")
        if index >= MAX_EDITS:
            _reject(result, edit, f"more than {MAX_EDITS} edits in one attempt")
            continue
        target = _resolve(root, rel)
        if target is None:
            _reject(result, edit, "path is outside the worktree")
            continue
        if target.name in PROTECTED_NAMES:
            _reject(result, edit, "manifests and lockfiles are changed by the version bump, not by edits")
            continue
        if not path_allowed(rel, allowed_paths):
            _reject(result, edit, "file is outside the allowed_paths of .codebase-pilot.yml")
            continue
        if rel not in scope and not is_test_file(rel):
            _reject(result, edit, "file is not in the package scope")
            continue
        if not target.is_file():
            _reject(result, edit, "file does not exist")
            continue
        if not edit.find:
            _reject(result, edit, "empty find text")
            continue
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(edit.find)
        if occurrences != 1:
            _reject(result, edit, f"find text occurs {occurrences} times, expected exactly once")
            continue
        lines = _changed_lines(edit)
        if changed_lines + lines > MAX_CHANGED_LINES:
            _reject(result, edit, f"would exceed {MAX_CHANGED_LINES} changed lines")
            continue
        originals.setdefault(target, text)
        target.write_text(text.replace(edit.find, edit.replace, 1), encoding="utf-8")
        changed_lines += lines
        result.applied.append(edit)

    if syntax_check:
        for target, original in originals.items():
            if target.suffix not in SYNTAX_CHECKED:
                continue
            problem = node_check(target)
            if problem is None:
                continue
            target.write_text(original, encoding="utf-8")  # revert every edit to this file
            rel = target.relative_to(root.resolve()).as_posix()
            reverted = [e for e in result.applied if e.file.replace("\\", "/") == rel]
            result.applied = [e for e in result.applied if e not in reverted]
            for edit in reverted:
                _reject(result, edit, f"syntax error after the edit: {problem.splitlines()[0] if problem else ''}")
    return result
