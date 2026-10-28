"""Precision and recall of impact findings against the answer key (BS 10).

    python evals/run_eval.py --db demo.db [--run <upgrade_run_id>] [--min-recall 1.0 --min-precision 0.9]

Run from the repository root. Exits 1 when a threshold is missed.
"""
import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core import db as core_db  # noqa: E402
from app.orchestrator import store as run_store  # noqa: E402

DEFAULT_KEY = ROOT / "evals" / "sandbox-shop.answer_key.yaml"


def load_key(path: Path = DEFAULT_KEY) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def evaluate(findings, key: dict) -> dict:
    """Findings the LLM rejected (confirmed=False) don't count as reported."""
    expected = {(f["file"], f["line"]) for f in key["expected_findings"]}
    reported = {(f.file_path, f.line) for f in findings if f.confirmed is not False}
    hit = expected & reported
    negatives = set(key.get("negatives", []))
    return {
        "expected": len(expected),
        "reported": len(reported),
        "recall": len(hit) / len(expected) if expected else 1.0,
        "precision": len(hit) / len(reported) if reported else 1.0,
        "missed": sorted(expected - reported),
        "extra": sorted(reported - expected),
        "negatives_reported": sorted(p for p, _ in reported if p in negatives),
    }


def quote_validation_rate(conn, run_id: str) -> float:
    """Share of displayed changes whose quote is in the stored release notes (should be 1.0 by construction)."""
    from app.updates.evidence import quote_in_notes

    run = run_store.get_run(conn, run_id)
    rows = conn.execute(
        "SELECT c.evidence_quote, c.version FROM update_changes c JOIN dependency_updates d ON d.analysis_id = c.analysis_id "
        "WHERE d.id = ?", (run["dependency_update_id"],),
    ).fetchall()
    if not rows:
        return 1.0
    ok = 0
    for r in rows:
        note = conn.execute(
            "SELECT body_md FROM release_notes WHERE name = ? AND version = ?", (run["package"], r["version"])
        ).fetchone()
        ok += bool(note and quote_in_notes(r["evidence_quote"], note["body_md"]))
    return ok / len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--run")
    parser.add_argument("--key", default=str(DEFAULT_KEY))
    parser.add_argument("--min-recall", type=float, default=1.0)
    parser.add_argument("--min-precision", type=float, default=0.9)
    args = parser.parse_args(argv)

    conn = core_db.connect(args.db)
    runs = [run_store.get_run(conn, args.run)] if args.run else [r for r in run_store.list_runs(conn) if r["package"] == "express"]
    if not runs or runs[0] is None:
        print("no upgrade run found", file=sys.stderr)
        return 1
    run = runs[-1]
    result = evaluate(run_store.load_findings(conn, run["id"]), load_key(Path(args.key)))
    result["quote_validation_rate"] = quote_validation_rate(conn, run["id"])
    for name, value in result.items():
        print(f"{name}: {value}")
    ok = result["recall"] >= args.min_recall and result["precision"] >= args.min_precision
    ok = ok and not result["negatives_reported"] and result["quote_validation_rate"] == 1.0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
