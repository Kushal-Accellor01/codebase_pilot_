"""Confluence Cloud: publish a report as one page (create, then update by version) and a connection test.

REST v2 (checked against Atlassian's docs): `POST /wiki/api/v2/pages` takes `spaceId` (required), `status`, `title`,
`parentId`, `body: {representation: "storage", value}`; `PUT /wiki/api/v2/pages/{id}` takes `id`, `status`, `title`,
`body` and `version: {number}` (all required, number = current + 1). Basic auth: account email and API token.
"""
import base64

import httpx

from app.connectors.base import TestResult, redact
from app.updates.http import HttpClient, HttpError


def _headers(email: str, token: str) -> dict[str, str]:
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {basic}", "Accept": "application/json", "Content-Type": "application/json"}


def _base(base_url: str) -> str:
    return base_url.rstrip("/") + "/wiki/api/v2"


async def test_connection(http: HttpClient, *, base_url: str | None, email: str | None, token: str | None,
                          space_id: str | None) -> TestResult:
    if not (base_url and email and token):
        return TestResult(
            False, "CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN must be set on the server")
    try:
        resp = await http.get("confluence", f"{_base(base_url)}/spaces", headers=_headers(email, token),
                              params={"limit": 1})
    except (httpx.HTTPError, HttpError) as exc:
        return TestResult(False, redact(f"Could not reach Confluence: {type(exc).__name__}", token))
    if resp.status_code in (401, 403):
        return TestResult(False, "Confluence rejected the email or API token")
    if resp.status_code != 200:
        return TestResult(False, f"Confluence answered {resp.status_code}")
    if not space_id:
        return TestResult(False, "Signed in, but no space id is set for the report page")
    return TestResult(True, "Signed in to Confluence")


async def publish_page(
    http: HttpClient, *, base_url: str, email: str, token: str, space_id: str, title: str, html: str,
    page_id: str | None = None, parent_id: str | None = None,
) -> tuple[str, str]:
    """(page id, 'created' | 'updated'). Updates the page when `page_id` is known and still exists."""
    headers, api = _headers(email, token), _base(base_url)
    body = {"representation": "storage", "value": html}
    if page_id:
        current = await http.get("confluence", f"{api}/pages/{page_id}", headers=headers)
        if current.status_code == 200:
            version = int(current.json()["version"]["number"]) + 1
            resp = await http.request("confluence", "PUT", f"{api}/pages/{page_id}", headers=headers, json={
                "id": page_id, "status": "current", "title": title, "body": body,
                "version": {"number": version, "message": "Updated by Codebase Pilot"}})
            resp.raise_for_status()
            return page_id, "updated"
        if current.status_code != 404:
            current.raise_for_status()  # the page vanished (404): create a new one below
    payload = {"spaceId": space_id, "status": "current", "title": title, "body": body}
    if parent_id:
        payload["parentId"] = parent_id
    resp = await http.post("confluence", f"{api}/pages", headers=headers, json=payload)
    resp.raise_for_status()
    return str(resp.json()["id"]), "created"


def report_html(repo: str, rows: list[dict], vulnerabilities: dict, dashboard_url: str | None) -> str:
    """The report page in Confluence storage format. Plain facts and counts: no code and no diffs."""
    from html import escape

    cells = "".join(
        f"<tr><td>{escape(r['package'])}</td>"
        f"<td>{escape(str(r['from']))} to {escape(str(r['to']))}</td>"
        f"<td>{escape(r['risk'] or '-')}</td><td>{escape(r['status_label'])}</td></tr>"
        for r in rows)
    link = f'<p><a href="{escape(dashboard_url)}">Open in Codebase Pilot</a></p>' if dashboard_url else ""
    return (
        f"<h1>Dependency report: {escape(repo)}</h1>"
        f"<p>{vulnerabilities['all']} known advisories, {vulnerabilities['applies']} apply to the code.</p>"
        "<h2>Upgrades</h2><table><tbody><tr><th>Package</th><th>Change</th><th>Risk</th><th>Status</th></tr>"
        f"{cells}</tbody></table>{link}"
    )
