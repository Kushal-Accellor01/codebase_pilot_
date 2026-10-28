"""A local git mirror of pilot-sandbox-shop for offline demos and tests (A3).

    python scripts/make_sandbox_mirror.py [--dest workspace/origin/pilot-sandbox-shop]

Copies the sandbox app (without node_modules), `git init -b main`, commits as PILOT_GIT_AUTHOR and prints the
path. Pilot can ingest this path like a URL, and the local PR fallback pushes its branches back into it.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "pilot-sandbox-shop"
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402


def parse_author(text: str) -> tuple[str, str]:
    m = re.match(r"\s*(.*?)\s*<(.+?)>\s*$", text)
    return (m.group(1) or "Codebase Pilot", m.group(2)) if m else (text.strip() or "Codebase Pilot", "pilot@localhost")


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def remove_tree(path: Path) -> None:
    import os
    import stat

    def writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def make_mirror(dest: Path) -> Path:
    dest = dest.resolve()
    if dest.exists():
        remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SANDBOX, dest, ignore=shutil.ignore_patterns("node_modules"))
    name, email = parse_author(get_settings().pilot_git_author)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.name", name)
    git(dest, "config", "user.email", email)
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "pilot-sandbox-shop (Express 4.21.2)")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    default = Path(get_settings().codebase_pilot_workspace) / "origin" / "pilot-sandbox-shop"
    parser.add_argument("--dest", default=str(default))
    args = parser.parse_args(argv)
    print(make_mirror(Path(args.dest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
