"""python -m app.updates.cli <command>  (run from backend/)"""
import argparse
import sys
from collections import Counter

from app.core.logging import configure_logging
from app.updates import store
from app.updates.config import default_db_path
from app.updates.preflight import PreflightError, resolve_scan, run_preflight


def _cmd_migrate(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        store.migrate(conn)
    finally:
        conn.close()
    print(f"Migration 002 applied to {args.db}")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        report = run_preflight(conn, args.scan)
        undeclared = store.count_undeclared_imports(conn, report.scan_id)
    finally:
        conn.close()
    print(f"Scan:                           {report.scan_id}")
    print(f"Dependencies:                   {report.dependencies}")
    print(f"Declared without constraint:    {report.declared_without_constraint}")
    print(f"Missing dependency_type:        {report.missing_dependency_type}")
    print(f"Lockfiles present:              {report.lockfiles}")
    print(f"Undeclared imports (info):      {undeclared}")
    for w in report.warnings:
        print(f"WARN: {w}")
    if report.lockfiles == 0:
        print("INFO: no lockfile, current versions are resolved from the declared range")
    return 0


def _cmd_inventory(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        deps = store.load_inventory(conn, scan_id)
    finally:
        conn.close()
    print(f"Scan {scan_id}: {len(deps)} dependencies")
    print(f"By type: {dict(Counter(d.dependency_type for d in deps))}")
    for d in deps:
        constraint = d.declared_constraint if d.declared_constraint is not None else "-"
        print(f"{d.importer_count:>4}  {d.ecosystem:<5} {d.dependency_type:<8} {d.name} {constraint}")
    return 0


async def _fetch_registry(args: argparse.Namespace) -> int:
    from app.updates.http import HttpClient
    from app.updates.registry.batch import fetch_many
    from app.updates.registry.npm import NpmRegistry

    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        names = sorted({d.name for d in store.load_inventory(conn, scan_id) if d.ecosystem == "npm"})
        if args.only:
            wanted = set(args.only.split(","))
            names = [n for n in names if n in wanted]
        async with HttpClient() as http:
            batch = await fetch_many(NpmRegistry(http), conn, names, refresh=args.refresh)
        for name in names:
            if name in batch.errors:
                print(f"{name:<28} ERROR {batch.errors[name]}")
                continue
            info = batch.packages[name]
            flag = " DEPRECATED" if info.is_deprecated else ""
            print(f"{name:<28} {info.status:<10} latest={info.latest_version} versions={len(info.versions)}{flag}")
    finally:
        conn.close()
    return 0


async def _show_versions(args: argparse.Namespace) -> int:
    from app.updates.http import HttpClient
    from app.updates.registry.batch import fetch_many
    from app.updates.registry.npm import NpmRegistry
    from app.updates.versions import parse_scan_time, resolve_versions

    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        scan_time = parse_scan_time(store.scan_scanned_at(conn, scan_id))
        deps = [d for d in store.load_inventory(conn, scan_id) if d.ecosystem == "npm"]
        async with HttpClient() as http:
            batch = await fetch_many(NpmRegistry(http), conn, [d.name for d in deps])
        header = ("package", "declared", "current", "basis", "in-range", "latest")
        print("".join(f"{h:<{w}}" for h, w in zip(header, (24, 14, 10, 24, 10, 10))) + "update  behind")
        for d in deps:
            declared = d.declared_constraint or "-"
            if d.name in batch.errors:
                print(f"{d.name:<24}{declared:<14}ERROR {batch.errors[d.name]}")
                continue
            info = batch.packages[d.name]
            if info.status != "ok":
                print(f"{d.name:<24}{declared:<14}{info.status}")
                continue
            v = resolve_versions(d.declared_constraint, info, scan_time)
            if v.error:
                print(f"{d.name:<24}{declared:<14}ERROR {v.error}")
                continue
            flags = (" MANIFEST" if v.requires_manifest_change else "") + (
                " NODE" if v.runtime_requirement_change else "") + (" DEPRECATED" if v.is_deprecated else "")
            print(
                f"{d.name:<24}{declared:<14}{v.current_version:<10}"
                f"{v.current_version_basis:<24}{v.latest_in_range or '-':<10}{v.latest_version or '-':<10}"
                f"{v.update_type:<8}{v.versions_behind}{flags}"
            )
    finally:
        conn.close()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import json

    from app.updates.pipeline import run_dependency_updates

    only = [n for n in args.only.split(",") if n] if args.only else None
    run_id, stats = run_dependency_updates(
        args.db, args.scan, only=only, refresh=args.refresh, security=not args.skip_security,
        notes=not args.skip_notes, analysis=not args.no_llm, write_report=not args.no_report,
        reports_dir=args.reports_dir,
    )
    conn = store.connect(args.db)
    try:
        run = store.get_pipeline_run(conn, run_id)
        rows = store.list_dependency_updates(conn, run["scan_id"])
    finally:
        conn.close()
    print(f"Run {run_id}: {run['status']} (scan {run['scan_id']})")
    print(json.dumps(stats.model_dump(), indent=2))
    shown = [r for r in rows if r["status"] != "up_to_date"][:15]
    if shown:
        print(f"\nTop {len(shown)} by priority:")
        for r in shown:
            move = f"{r['current_version']} -> {r['recommended_target']}" if r["current_version"] else "-"
            print(f"{r['priority_score'] or 0:>6}  {r['risk_level'] or '-':<8} {r['name']:<24} {move:<22} "
                  f"{r['update_type'] or '-':<7} {r['status']}")
    return 1 if run["status"] == "failed" else 0


def _cmd_report(args: argparse.Namespace) -> int:
    from app.updates.report import render_json, render_markdown

    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        print(render_markdown(conn, scan_id) if args.format == "md" else render_json(conn, scan_id))
    finally:
        conn.close()
    return 0


def _cmd_versions(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_show_versions(args))


def _cmd_registry(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_fetch_registry(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.updates.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_ in (
        ("migrate", _cmd_migrate, "apply migration 002"),
        ("preflight", _cmd_preflight, "check the latest scan"),
        ("inventory", _cmd_inventory, "print the dependency inventory"),
        ("versions", _cmd_versions, "resolve current / latest versions for the scan's npm dependencies"),
        ("run", _cmd_run, "run the dependency-updates pipeline for a scan"),
        ("report", _cmd_report, "print the report for a scan"),
        ("registry", _cmd_registry, "fetch npm registry data for the scan's dependencies into the cache"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", default=default_db_path())
        p.add_argument("--verbose", "-v", action="store_true", help="log one line per package to stderr")
        if name != "migrate":
            p.add_argument("--scan", default="latest")
        if name in ("registry", "run"):
            p.add_argument("--only", help="comma-separated package names")
            p.add_argument("--refresh", action="store_true", help="ignore the cache")
        if name == "run":
            p.add_argument("--no-llm", action="store_true", help="fetch notes but skip LLM analysis")
            p.add_argument("--skip-security", action="store_true", help="do not query OSV (offline runs)")
            p.add_argument("--skip-notes", action="store_true", help="no GitHub: skip source and release notes")
            p.add_argument("--no-report", action="store_true", help="do not write reports/*.json and *.md")
            p.add_argument("--reports-dir", default=None, help="where to write reports (default: reports/)")
        if name == "report":
            p.add_argument("--format", choices=["md", "json"], default="md")
        p.set_defaults(func=fn)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("INFO" if args.verbose else "WARNING")
    try:
        return args.func(args)
    except (PreflightError, store.StoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
