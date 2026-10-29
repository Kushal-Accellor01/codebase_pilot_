"""S5: Dependabot security alerts as a change source and as a second opinion on the advisories of a scan.

`GET /repos/{owner}/{repo}/dependabot/alerts?state=open&ecosystem=npm&per_page=100`, following the cursor in the
`Link` header. Fields used (GitHub REST docs, "REST API endpoints for Dependabot alerts"): `number`, `state`,
`html_url`, `dependency.package.{ecosystem,name}`, `dependency.scope`, `security_advisory.{ghsa_id,cve_id,summary,
severity}`, `security_vulnerability.{vulnerable_version_range,first_patched_version.identifier,severity}`.

Alerts need the token permission "Dependabot alerts: read". A 403 or 404 means alerts are off for the repository or
the token cannot read them: the channel says `needs_permission` with the reason, and nothing else fails.
"""
import logging
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlparse

from nodesemver import satisfies

from app.changes import store as change_store
from app.channels import store as channel_store
from app.core.contracts import ChangeEvent, ChangeItem, ChangeSubject, Severity, VulnRef
from app.scan import store as scan_store
from app.scan import vulns as scan_vulns
from app.updates.github import GitHubClient

logger = logging.getLogger(__name__)

SEVERITIES = {"critical", "high", "medium", "low"}
MAX_PAGES = 20
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class AlertsUnavailable(Exception):
    """Dependabot alerts cannot be read for this repository; `str(exc)` says why in one sentence."""


def _severity(value: str | None) -> Severity:
    value = (value or "").lower()
    return value if value in SEVERITIES else "none"  # type: ignore[return-value]


async def fetch_alerts(client: GitHubClient, slug: str) -> list[dict]:
    """Every open npm alert, all pages. Raises AlertsUnavailable on 403/404."""
    alerts: list[dict] = []
    path, params = f"repos/{slug}/dependabot/alerts", {"state": "open", "ecosystem": "npm", "per_page": 100}
    for _ in range(MAX_PAGES):
        resp = await client.request(path, params=params)
        if resp.status_code in (403, 404):
            raise AlertsUnavailable(
                "Dependabot alerts are off for this repository, or the token lacks 'Dependabot alerts: read'"
                f" (HTTP {resp.status_code})")
        resp.raise_for_status()
        page = resp.json()
        alerts += [a for a in page if isinstance(a, dict)] if isinstance(page, list) else []
        link = _NEXT.search(resp.headers.get("link", ""))
        if not link:
            return alerts
        # The Link header carries the cursor; follow its query (the path stays the same).
        params = dict(parse_qsl(urlparse(link.group(1)).query))
    return alerts


def _ids(alert: dict) -> set[str]:
    advisory = alert.get("security_advisory") or {}
    return {i for i in (advisory.get("ghsa_id"), advisory.get("cve_id")) if i}


def _in_range(version: str, vulnerable_range: str | None) -> bool:
    """Does `version` fall in GitHub's range text (">= 1.0.0, < 2.0.0")? Unparseable ranges match every copy."""
    if not vulnerable_range:
        return True
    try:
        return bool(satisfies(version, " ".join(part.strip() for part in vulnerable_range.split(","))))
    except Exception:
        return True


def merge_into_scan(conn: sqlite3.Connection, scan_id: str, alerts: list[dict]) -> dict[str, int]:
    """Add Dependabot as a source on advisories OSV already reported; add the ones OSV did not report."""
    merged = added = 0
    components = scan_store.load_components(conn, scan_id)
    existing = scan_store.list_scan_vulns(conn, scan_id)
    imported = scan_vulns.imported_packages(conn, scan_id)
    for alert in alerts:
        package = ((alert.get("dependency") or {}).get("package") or {}).get("name")
        if not package:
            continue
        ids = _ids(alert)
        hit = next((v for v in existing if v["name"] == package and (v["vuln_id"] in ids or ids & set(v["aliases"]))),
                   None)
        if hit:
            if scan_store.add_vuln_source(conn, scan_id, hit["vuln_id"], package, "dependabot"):
                merged += 1
            continue
        vulnerability = alert.get("security_vulnerability") or {}
        affected = [c for c in components if c.name == package
                    and _in_range(c.version, vulnerability.get("vulnerable_version_range"))]
        advisory = alert.get("security_advisory") or {}
        vuln_id = advisory.get("ghsa_id") or advisory.get("cve_id") or f"dependabot-{alert.get('number')}"
        fixed = (vulnerability.get("first_patched_version") or {}).get("identifier")
        for component in affected[:1]:  # one row per package, like OSV rows
            decided, reason = scan_vulns.verdict(component, imported)
            row = {
                "vuln_id": vuln_id, "name": package, "version": component.version, "purl": component.purl,
                "scope": component.scope, "direct": component.direct, "path": component.path,
                "severity": _severity(vulnerability.get("severity") or advisory.get("severity")),
                "summary": advisory.get("summary"), "aliases": sorted(ids - {vuln_id}), "fixed_in": fixed,
                "verdict": decided, "reason": reason, "sources": ["dependabot"], "url": alert.get("html_url"),
            }
            scan_store.insert_scan_vuln(conn, scan_id, row)
            added += 1
    return {"merged": merged, "added": added}


def alert_event(repo: str, scan_id: str | None, alert: dict, version: str | None) -> ChangeEvent | None:
    """One external change event per alert (origin external, source dependabot_alert)."""
    package = ((alert.get("dependency") or {}).get("package") or {}).get("name")
    advisory = alert.get("security_advisory") or {}
    vulnerability = alert.get("security_vulnerability") or {}
    if not package or not advisory:
        return None
    vuln_id = advisory.get("ghsa_id") or f"dependabot-{alert.get('number')}"
    fixed = (vulnerability.get("first_patched_version") or {}).get("identifier")
    summary = advisory.get("summary") or vuln_id
    severity = _severity(vulnerability.get("severity") or advisory.get("severity"))
    return ChangeEvent(
        event_id=change_store.make_event_id("dependabot_alert", repo, "package", package, version, vuln_id),
        origin="external", source="dependabot_alert", repo=repo, scan_id=scan_id,
        subject=ChangeSubject(kind="package", ecosystem="npm", name=package),
        from_version=version, to_version=fixed,
        detected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        items=[ChangeItem(kind="security", summary=summary, quote=summary, evidence_url=alert.get("html_url"),
                          verified=True)],  # made by code from the alert itself
        vulnerabilities=[VulnRef(id=vuln_id, severity=severity if severity != "none" else "unknown", summary=summary,
                                 fixed_in=fixed, url=alert.get("html_url"))],
        source_ref=alert.get("html_url"), severity_hint=severity,
    )


async def sync_scan(conn: sqlite3.Connection, client: GitHubClient, repo_slug: str, repo: str, scan_id: str) -> dict:
    """Read the alerts, merge them into the scan's advisories, record events and the channel status.

    Never raises for an unreadable alert list: the channel records why and the scan goes on."""
    try:
        alerts = await fetch_alerts(client, repo_slug)
    except AlertsUnavailable as exc:
        channel_store.set_status(conn, repo, "dependabot", "needs_permission", str(exc))
        return {"status": "needs_permission", "reason": str(exc), "alerts": 0, "merged": 0, "added": 0}
    counts = merge_into_scan(conn, scan_id, alerts)
    versions = {c.name: c.version for c in scan_store.load_components(conn, scan_id)}
    new = 0
    for alert in alerts:
        package = ((alert.get("dependency") or {}).get("package") or {}).get("name")
        event = alert_event(repo, scan_id, alert, versions.get(package or ""))
        if event and change_store.upsert_event(conn, event):
            new += 1
    channel_store.set_status(conn, repo, "dependabot", "connected", None)
    return {"status": "connected", "alerts": len(alerts), "new_events": new, **counts}


class DependabotAlertsSource:
    """The ChangeSource form of the above, for the scheduler: alerts become events, with no scan to merge into."""

    kind = "dependabot_alert"
    origin = "external"

    async def poll(self, ctx, repo: str, since: str | None) -> list[ChangeEvent]:
        from app.core.workspace import repo_identity

        slug = repo_identity(repo) or repo
        alerts = await fetch_alerts(GitHubClient(ctx.http), slug)
        events = [alert_event(repo, None, a, None) for a in alerts]
        return [e for e in events if e is not None]

    def from_webhook(self, headers, body) -> list[ChangeEvent] | None:
        return None  # alerts are polled; GitHub's `dependabot_alert` webhook is not used
