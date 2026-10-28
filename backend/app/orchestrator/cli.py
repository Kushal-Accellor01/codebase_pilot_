"""python -m app.orchestrator.cli <command>   (run from backend/)

pipeline <repo_url_or_path> [--only express]   ingest -> updates -> upgrade runs, stops at the approval pause
approve|reject <run_id> --by NAME [--comment]  decide a run that awaits approval
worker [--until-idle]                          process queued jobs
runs                                           list upgrade runs
reset-demo                                     clear run data, checkpoints, worktrees and local PR branches
"""
import argparse
import asyncio
import sys

from app.core import db as core_db
from app.core import jobs
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.orchestrator import demo
from app.orchestrator import store as run_store
from app.orchestrator.worker import Services, run_worker


def _db(args: argparse.Namespace) -> str:
    return args.db or get_settings().codebase_pilot_db


def _ensure_db(path: str) -> None:
    """The schema must exist before jobs can be queued (ingestion creates the first three tables)."""
    from app.database import GraphDatabase

    GraphDatabase(path).close()
    conn = core_db.connect(path)
    try:
        core_db.migrate(conn)
    finally:
        conn.close()


def _print_runs(db_path: str) -> None:
    conn = core_db.connect(db_path)
    try:
        for r in run_store.list_runs(conn):
            move = f"{r['package']} {r['from_version']} -> {r['to_version']}"
            print(f"{r['id']}  {r['status']:<18} {r['risk_level'] or '-':<9} {move}")
    finally:
        conn.close()


def _cmd_pipeline(args: argparse.Namespace) -> int:
    db = _db(args)
    _ensure_db(db)
    conn = core_db.connect(db)
    try:
        jobs.enqueue(conn, "scan", {"repo_url": args.repo, "only": args.only.split(",") if args.only else None})
    finally:
        conn.close()
    handled = asyncio.run(run_worker(db, until_idle=True, services=Services()))
    print(f"{handled} jobs processed")
    _print_runs(db)
    return 0


def _cmd_decide(args: argparse.Namespace, decision: str) -> int:
    db = _db(args)
    conn = core_db.connect(db)
    try:
        run = run_store.get_run(conn, args.run_id)
        if run is None:
            print(f"ERROR: unknown run {args.run_id}", file=sys.stderr)
            return 1
        if run["status"] != "awaiting_approval":
            print(f"ERROR: run is {run['status']}, not awaiting approval", file=sys.stderr)
            return 1
        jobs.enqueue(conn, "resume_upgrade", {"run_id": args.run_id, "decision": decision, "decided_by": args.by,
                                              "comment": args.comment})
    finally:
        conn.close()
    asyncio.run(run_worker(db, until_idle=True, services=Services()))
    _print_runs(db)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    handled = asyncio.run(run_worker(_db(args), until_idle=args.until_idle, services=Services()))
    print(f"{handled} jobs processed")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    _print_runs(_db(args))
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    print(demo.reset_demo(_db(args)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.orchestrator.cli")
    parser.add_argument("--db", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pipeline")
    p.add_argument("repo")
    p.add_argument("--only")
    p.set_defaults(func=_cmd_pipeline)
    for name, decision in (("approve", "approved"), ("reject", "rejected")):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        p.add_argument("--by", required=True)
        p.add_argument("--comment")
        p.set_defaults(func=lambda a, d=decision: _cmd_decide(a, d))
    p = sub.add_parser("worker")
    p.add_argument("--until-idle", action="store_true")
    p.set_defaults(func=_cmd_worker)
    sub.add_parser("runs").set_defaults(func=_cmd_runs)
    sub.add_parser("reset-demo").set_defaults(func=_cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("INFO" if args.verbose else "WARNING")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
