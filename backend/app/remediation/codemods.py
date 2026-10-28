"""Official codemods, run before any LLM edit because they are deterministic (BS 7.5 step 1).

A codemod is registered only after its npm package name and command have been confirmed for that exact
upgrade and its version pinned. Nothing is registered yet: the Express 4 -> 5 codemod package must be
verified first, so Pilot falls back to LLM edits.
"""
from dataclasses import dataclass
from pathlib import Path

from app.core import workspace


@dataclass(frozen=True)
class Codemod:
    package: str
    from_major: int
    to_major: int
    args: tuple[str, ...]  # full command, e.g. ("npx", "--yes", "@scope/codemod@1.2.3", "v5", ".")


# (npm package being upgraded) -> codemods for it. Fill in only verified entries.
CODEMODS: dict[str, list[Codemod]] = {}


def find_codemod(package: str, current: str, target: str) -> Codemod | None:
    try:
        from_major, to_major = int(current.split(".")[0]), int(target.split(".")[0])
    except ValueError:
        return None
    return next(
        (c for c in CODEMODS.get(package, []) if c.from_major == from_major and c.to_major == to_major), None
    )


def run_codemod(worktree: Path, repo: str | None, codemod: Codemod) -> workspace.CommandResult:
    workspace.assert_exec_allowed(repo)
    return workspace.run(list(codemod.args), worktree, timeout_s=workspace.INSTALL_TIMEOUT_S)
