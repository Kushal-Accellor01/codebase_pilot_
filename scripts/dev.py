"""Developer tasks that work the same on Windows and Linux (F1).  Run from anywhere:

    python scripts/dev.py seed-demo        scan the sandbox mirror for real (needs GITHUB_TOKEN + Azure OpenAI)
    python scripts/dev.py offline-demo     the same product with a scripted model: no keys, no network
    python scripts/dev.py api              API on :8000 (worker in the process when PILOT_EMBEDDED_WORKER=1)
    python scripts/dev.py web              dashboard on :5173
    python scripts/dev.py reset-demo       clear runs, checkpoints, worktrees and local PR branches
    python scripts/dev.py eval             precision / recall against the answer key

Add --dry-run to print the commands instead of running them.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PY = sys.executable


def mirror_path() -> Path:
    return Path(os.environ.get("CODEBASE_PILOT_WORKSPACE", BACKEND / "workspace")) / "origin" / "pilot-sandbox-shop"


def plan(command: str, env: dict[str, str]) -> list[tuple[list[str], Path, dict[str, str]]]:
    """(argv, cwd, extra env) steps for a subcommand."""
    mirror = mirror_path()
    if command == "seed-demo":
        allowed = env.get("EXEC_ALLOWED_REPOS") or str(mirror)
        extra = {"EXEC_ALLOWED_REPOS": allowed}
        return [
            ([PY, "scripts/make_sandbox_mirror.py", "--dest", str(mirror)], BACKEND, extra),
            ([PY, "-m", "app.orchestrator.cli", "pipeline", str(mirror), "--only", "express"], BACKEND, extra),
        ]
    if command == "offline-demo":
        return [([PY, "scripts/demo_server.py", "--port", "8000"], BACKEND, {"PYTHONUTF8": "1"})]
    if command == "api":
        return [([PY, "-m", "uvicorn", "app.api.main:app", "--port", "8000", "--reload"], BACKEND, {})]
    if command == "web":
        npm = shutil.which("npm") or "npm"
        return [([npm, "run", "dev"], FRONTEND, {})]
    if command == "reset-demo":
        return [([PY, "-m", "app.orchestrator.cli", "reset-demo"], BACKEND, {})]
    if command == "eval":
        db = env.get("CODEBASE_PILOT_DB", str(BACKEND / "codebase_pilot.db"))
        return [([PY, "evals/run_eval.py", "--db", db], ROOT, {})]
    raise SystemExit(f"unknown command {command}")


COMMANDS = ("seed-demo", "offline-demo", "api", "web", "reset-demo", "eval")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    for argv_, cwd, extra in plan(args.command, dict(os.environ)):
        if args.dry_run:
            print(f"(cd {cwd}) {' '.join(argv_)}  env+={extra}")
            continue
        result = subprocess.run(argv_, cwd=cwd, env={**os.environ, **extra})
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
