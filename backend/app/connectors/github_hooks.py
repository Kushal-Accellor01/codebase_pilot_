"""Install the GitHub push webhook (S6). Idempotent: an existing hook with the same URL is reused.

`POST /repos/{owner}/{repo}/hooks` with `{"name": "web", "config": {"url", "content_type": "json", "secret"},
"events": ["push"], "active": true}`; the token needs "Webhooks: read and write" (fine-grained) or `admin:repo_hook`.
"""
from app.connectors.base import TestResult
from app.updates.github import GitHubClient

WEBHOOK_PATH = "/api/webhooks/github"


def hook_url(public_url: str) -> str:
    return public_url.rstrip("/") + WEBHOOK_PATH


async def find_hook(client: GitHubClient, slug: str, url: str) -> dict | None:
    status, hooks = await client.request_json("GET", f"repos/{slug}/hooks?per_page=100")
    if status != 200 or not isinstance(hooks, list):
        return None
    return next((h for h in hooks if (h.get("config") or {}).get("url") == url), None)


async def install(client: GitHubClient, slug: str, public_url: str | None, secret: str | None) -> TestResult:
    if not public_url:
        return TestResult(False, "PILOT_PUBLIC_URL is not set, so GitHub has no address to call")
    if not secret:
        return TestResult(False, "GITHUB_WEBHOOK_SECRET is not set on the server")
    url = hook_url(public_url)
    status, probe = await client.request_json("GET", f"repos/{slug}/hooks?per_page=100")
    if status in (403, 404):
        return TestResult(False, "The token cannot manage webhooks on this repository (needs Webhooks: read and write)")
    hooks = probe if isinstance(probe, list) else []
    existing = next((h for h in hooks if (h.get("config") or {}).get("url") == url), None)
    if existing:
        return TestResult(True, f"The webhook was already installed (hook {existing.get('id')})")
    status, created = await client.request_json("POST", f"repos/{slug}/hooks", {
        "name": "web", "active": True, "events": ["push"],
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}})
    if status not in (200, 201):
        message = created.get("message") if isinstance(created, dict) else None
        return TestResult(False, f"GitHub refused to create the webhook: {message or status}")
    return TestResult(True, f"Webhook installed: pushes to {slug} now start a scan")


async def ping(client: GitHubClient, slug: str, public_url: str | None) -> TestResult:
    """Ask GitHub to deliver a `ping` to the installed hook."""
    if not public_url:
        return TestResult(False, "PILOT_PUBLIC_URL is not set")
    hook = await find_hook(client, slug, hook_url(public_url))
    if hook is None:
        return TestResult(False, "The webhook is not installed yet")
    status, _ = await client.request_json("POST", f"repos/{slug}/hooks/{hook['id']}/pings")
    return TestResult(status in (200, 204), "GitHub was asked to send a ping" if status in (200, 204)
                      else f"GitHub answered {status}")
