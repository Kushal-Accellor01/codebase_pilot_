"""Known-vulnerability check against OSV.dev (S2 5.4): querybatch for ids, vulns/{id} for details."""
import asyncio
import logging
import math
import sqlite3
from dataclasses import dataclass
from functools import cmp_to_key

from app.updates import store
from app.updates.http import HttpClient
from app.updates.models import VulnRecord
from app.updates.versions import clean_version, compare_versions

logger = logging.getLogger(__name__)

OSV_BASE = "https://api.osv.dev/v1"
BATCH_SIZE = 1000  # OSV's documented limit per querybatch call
MAX_PAGES = 20

_SORT_KEY = cmp_to_key(compare_versions)
_GHSA_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}


@dataclass
class PackageVuln:
    id: str
    severity: str
    summary: str | None
    url: str
    fixed_in: str | None


# --- severity ---------------------------------------------------------------------------------

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _roundup(x: float) -> float:
    n = round(x * 100000)
    return n / 100000.0 if n % 10000 == 0 else (math.floor(n / 10000) + 1) / 10.0


def cvss3_base_score(vector: str) -> float | None:
    """CVSS v3.0 / v3.1 base score from a vector string, or None if it can't be parsed."""
    try:
        parts = dict(p.split(":", 1) for p in vector.split("/")[1:])
        scope_changed = parts["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[parts["PR"]]
        iss = 1 - (1 - _CIA[parts["C"]]) * (1 - _CIA[parts["I"]]) * (1 - _CIA[parts["A"]])
        impact = (
            7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        )
        exploitability = 8.22 * _AV[parts["AV"]] * _AC[parts["AC"]] * pr * _UI[parts["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    total = impact + exploitability
    return _roundup(min(1.08 * total if scope_changed else total, 10))


def severity_from_score(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


def severity_of(raw: dict) -> tuple[str, str | None]:
    """(severity, cvss_vector). GitHub's label wins; else CVSS v3 score; else 'unknown'."""
    vector = next(
        (s.get("score") for s in raw.get("severity", []) if str(s.get("type", "")).startswith("CVSS_V3")), None
    )
    label = str((raw.get("database_specific") or {}).get("severity", "")).upper()
    if label in _GHSA_SEVERITY:
        return _GHSA_SEVERITY[label], vector
    if vector:
        score = cvss3_base_score(vector)
        if score is not None:
            return severity_from_score(score), vector
    return "unknown", vector


# --- parsing ------------------------------------------------------------------------------------


def parse_vuln(raw: dict) -> VulnRecord:
    severity, vector = severity_of(raw)
    vuln_id = raw["id"]
    advisory = next((r["url"] for r in raw.get("references", []) if r.get("type") == "ADVISORY" and r.get("url")), None)
    return VulnRecord(
        id=vuln_id,
        aliases=raw.get("aliases", []),
        summary=raw.get("summary"),
        severity=severity,
        cvss_vector=vector,
        published_at=raw.get("published"),
        modified_at=raw.get("modified"),
        url=advisory or f"https://osv.dev/vulnerability/{vuln_id}",
        raw=raw,
    )


def fixed_in(raw: dict, name: str, current: str, ecosystem: str = "npm") -> str | None:
    """Lowest `fixed` version for this package that is greater than `current`, or None."""
    candidates: list[str] = []
    for affected in raw.get("affected", []):
        pkg = affected.get("package") or {}
        if pkg.get("name") != name or pkg.get("ecosystem") != ecosystem:
            continue
        for rng in affected.get("ranges", []):
            if rng.get("type") not in ("SEMVER", "ECOSYSTEM"):
                continue  # GIT ranges carry commit hashes
            for event in rng.get("events", []):
                version = clean_version(event["fixed"]) if "fixed" in event else None
                if version and compare_versions(version, current) > 0:
                    candidates.append(version)
    return min(candidates, key=_SORT_KEY) if candidates else None


def min_safe_version(vulns: list[PackageVuln]) -> str | None:
    """The lowest version that clears every vulnerability that has a fix."""
    fixes = [v.fixed_in for v in vulns if v.fixed_in]
    return max(fixes, key=_SORT_KEY) if fixes else None


# --- network ------------------------------------------------------------------------------------


async def query_batch(http: HttpClient, targets: list[tuple[str, str]], ecosystem: str = "npm") -> list[list[dict]]:
    """For each (name, version) the list of {id, modified} OSV reports, in the same order as `targets`.

    `version` is sent without a purl version (sending both is a 400). Results that carry a
    next_page_token are re-queried, only those entries, until the token runs out.
    """
    queries = [{"package": {"name": n, "ecosystem": ecosystem}, "version": v} for n, v in targets]
    found: list[list[dict]] = [[] for _ in targets]
    pending = list(range(len(queries)))
    tokens: dict[int, str] = {}

    for _ in range(MAX_PAGES):
        if not pending:
            break
        next_pending: list[int] = []
        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start : start + BATCH_SIZE]
            body = {"queries": [{**queries[i], **({"page_token": tokens[i]} if i in tokens else {})} for i in chunk]}
            resp = await http.post("osv", f"{OSV_BASE}/querybatch", json=body)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if len(results) != len(chunk):
                raise RuntimeError(f"OSV returned {len(results)} results for {len(chunk)} queries")
            for i, result in zip(chunk, results):
                found[i].extend(result.get("vulns", []))
                if result.get("next_page_token"):
                    tokens[i] = result["next_page_token"]
                    next_pending.append(i)
                else:
                    tokens.pop(i, None)
        pending = next_pending
    else:
        if pending:
            logger.warning("OSV pagination stopped after %d pages", MAX_PAGES)
    return found


async def fetch_vuln(http: HttpClient, vuln_id: str) -> dict:
    resp = await http.get("osv", f"{OSV_BASE}/vulns/{vuln_id}")
    resp.raise_for_status()
    return resp.json()


async def check_security(
    http: HttpClient,
    conn: sqlite3.Connection,
    targets: list[tuple[str, str]],
    ecosystem: str = "npm",
) -> dict[tuple[str, str], list[PackageVuln]]:
    """Vulnerabilities per (name, version) for the given targets.

    Details are cached in `vulnerabilities` and refetched only when OSV reports a newer `modified`.
    """
    targets = list(dict.fromkeys(targets))
    per_package = await query_batch(http, targets, ecosystem)

    reported: dict[str, str | None] = {}
    for hits in per_package:
        for hit in hits:
            reported[hit["id"]] = hit.get("modified")

    stale = [vid for vid, modified in reported.items() if _needs_fetch(conn, vid, modified)]

    async def one(vid: str) -> None:
        try:
            raw = await fetch_vuln(http, vid)
        except Exception as exc:
            logger.warning("could not fetch %s: %s", vid, exc)
            if store.load_vulnerability(conn, vid) is not None:
                return  # keep the older details
            # Keep the advisory visible with unknown severity rather than hiding it.
            raw = {"id": vid, "summary": "Details unavailable (OSV request failed)", "modified": reported[vid]}
        store.save_vulnerability(conn, parse_vuln(raw))

    await asyncio.gather(*(one(v) for v in stale))

    out: dict[tuple[str, str], list[PackageVuln]] = {}
    for (name, version), hits in zip(targets, per_package):
        vulns = []
        for vid in dict.fromkeys(h["id"] for h in hits):
            rec = store.load_vulnerability(conn, vid)
            vulns.append(
                PackageVuln(
                    id=vid,
                    severity=rec["severity"] or "unknown",
                    summary=rec["summary"],
                    url=rec["url"],
                    fixed_in=fixed_in(rec["raw"], name, version, ecosystem),
                )
            )
        out[(name, version)] = vulns
    return out


def _needs_fetch(conn: sqlite3.Connection, vuln_id: str, modified: str | None) -> bool:
    cached = store.load_vulnerability(conn, vuln_id)
    return cached is None or (modified is not None and cached["modified_at"] != modified)
