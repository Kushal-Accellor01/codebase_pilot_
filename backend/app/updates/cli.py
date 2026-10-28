"""python -m app.updates.cli <command>  (run from backend/)"""
import argparse
import sys
from collections import Counter

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
    from app.updates.registry.cache import get_package
    from app.updates.registry.npm import NpmRegistry

    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        names = sorted({d.name for d in store.load_inventory(conn, scan_id) if d.ecosystem == "npm"})
        if args.only:
            wanted = set(args.only.split(","))
            names = [n for n in names if n in wanted]
        async with HttpClient() as http:
            adapter = NpmRegistry(http)
            for name in names:
                try:
                    info = await get_package(adapter, conn, name, refresh=args.refresh)
                except Exception as exc:  # one package never stops the run
                    store.save_registry_failure(conn, "npm", name, str(exc))
                    print(f"{name:<28} ERROR {exc}")
                    continue
                flag = " DEPRECATED" if info.is_deprecated else ""
                print(f"{name:<28} {info.status:<10} latest={info.latest_version} versions={len(info.versions)}{flag}")
    finally:
        conn.close()
    return 0


async def _show_versions(args: argparse.Namespace) -> int:
    from app.updates.http import HttpClient
    from app.updates.registry.cache import get_package
    from app.updates.registry.npm import NpmRegistry
    from app.updates.versions import parse_scan_time, resolve_versions

    conn = store.connect(args.db)
    try:
        scan_id = resolve_scan(conn, args.scan)
        scan_time = parse_scan_time(store.scan_scanned_at(conn, scan_id))
        deps = [d for d in store.load_inventory(conn, scan_id) if d.ecosystem == "npm"]
        header = ("package", "declared", "current", "basis", "in-range", "latest")
        print("".join(f"{h:<{w}}" for h, w in zip(header, (24, 14, 10, 24, 10, 10))) + "update  behind")
        async with HttpClient() as http:
            adapter = NpmRegistry(http)
            for d in deps:
                try:
                    info = await get_package(adapter, conn, d.name)
                    if info.status != "ok":
                        print(f"{d.name:<24}{d.declared_constraint or '-':<14}{info.status}")
                        continue
                    v = resolve_versions(d.declared_constraint, info, scan_time)
                except Exception as exc:  # one package never stops the run
                    print(f"{d.name:<24}ERROR {exc}")
                    continue
                if v.error:
                    print(f"{d.name:<24}{d.declared_constraint or '-':<14}ERROR {v.error}")
                    continue
                flags = (" MANIFEST" if v.requires_manifest_change else "") + (
                    " NODE" if v.runtime_requirement_change else "") + (" DEPRECATED" if v.is_deprecated else "")
                print(
                    f"{d.name:<24}{d.declared_constraint or '-':<14}{v.current_version:<10}"
                    f"{v.current_version_basis:<24}{v.latest_in_range or '-':<10}{v.latest_version or '-':<10}"
                    f"{v.update_type:<8}{v.versions_behind}{flags}"
                )
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
        ("registry", _cmd_registry, "fetch npm registry data for the scan's dependencies into the cache"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", default=default_db_path())
        if name != "migrate":
            p.add_argument("--scan", default="latest")
        if name == "registry":
            p.add_argument("--only", help="comma-separated package names")
            p.add_argument("--refresh", action="store_true", help="ignore the cache")
        p.set_defaults(func=fn)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (PreflightError, store.StoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
