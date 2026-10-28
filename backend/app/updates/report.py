"""JSON and Markdown reports for a scan (S2 5.11 and 10.3)."""
import json
import sqlite3
from pathlib import Path

from app.core.config import get_settings
from app.updates import store
from app.updates.events import build_events


def _repo_label(info: dict) -> str:
    url = (info.get("repo_url") or "").rstrip("/")
    if url:
        return url.removeprefix("https://github.com/").removesuffix(".git")
    return Path((info.get("repository_path") or "unknown").replace("\\", "/")).name


def _cell(text: object) -> str:
    return str(text if text not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def render_markdown(conn: sqlite3.Connection, scan_id: str) -> str:
    info = store.get_scan_info(conn, scan_id)
    rows = store.list_dependency_updates(conn, scan_id)
    importers = store.importing_files_by_dep(conn, scan_id)
    events = {e.package: e for e in build_events(conn, scan_id, only_actionable=False)}

    updates = {"patch": 0, "minor": 0, "major": 0}
    for r in rows:
        if r["update_type"] in updates and r["status"] not in ("up_to_date", "error"):
            updates[r["update_type"]] += 1
    n_up = sum(1 for r in rows if r["status"] == "up_to_date")
    date = (info.get("scanned_at") or "")[:10]
    lines = [
        f"# Dependency updates: {_repo_label(info)} @ {(info.get('commit_sha') or '')[:7] or 'unknown'} "
        f"(scan {scan_id[:8]}, {date})",
        f"Totals: {len(rows)} dependencies · {n_up} up to date · "
        f"{sum(updates.values())} updates ({updates['patch']} patch / {updates['minor']} minor / "
        f"{updates['major']} major) · {sum(1 for r in rows if r['vuln_count'])} vulnerable · "
        f"{sum(1 for r in rows if r['is_deprecated'])} deprecated",
        "",
        "| Risk | Package | Type | Current → Target | Update | Vulns | Breaking changes | Used in | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        if r["status"] == "up_to_date":
            continue
        event = events.get(r["name"])
        breaking = sum(1 for c in (event.changes if event else []) if c.breaking)
        move = f"{r['current_version']} → {r['recommended_target']}" if r["current_version"] else "-"
        used = len(importers.get(r["dep_node_id"], []))
        lines.append(
            "| " + " | ".join(_cell(x) for x in (
                r["risk_level"], r["name"], r["dependency_type"], move, r["update_type"], r["vuln_count"],
                breaking if r["analysis_id"] else "-", f"{used} files", r["status"],
            )) + " |"
        )
    needs_help = [r for r in rows if r["status"] in ("source_unknown", "notes_unavailable", "error")]
    if needs_help:
        lines += ["", "## Needs a person", ""]
        lines += [f"- **{r['name']}**: {r['status']}" + (f" ({r['error']})" if r["error"] else "") for r in needs_help]
    return "\n".join(lines) + "\n"


def render_json(conn: sqlite3.Connection, scan_id: str) -> str:
    events = build_events(conn, scan_id)
    return json.dumps([e.model_dump(mode="json") for e in events], indent=2)


def write_reports(conn: sqlite3.Connection, scan_id: str, directory: str | Path | None = None) -> tuple[Path, Path]:
    """reports/dependency_updates_<scan_id>.json and .md"""
    out = Path(directory or get_settings().reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"dependency_updates_{scan_id}.json"
    md_path = out / f"dependency_updates_{scan_id}.md"
    json_path.write_text(render_json(conn, scan_id), encoding="utf-8")
    md_path.write_text(render_markdown(conn, scan_id), encoding="utf-8")
    return json_path, md_path
