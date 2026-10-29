"""Slack: notifications (`chat.postMessage` with a bot token, or an incoming webhook) and a connection test.

Bot token scopes: `chat:write` to post. Messages carry package, risk, counts and a link, never code or diffs.
"""
import httpx

from app.connectors.base import TestResult, redact
from app.updates.http import HttpClient, HttpError

API = "https://slack.com/api"


async def post_message(http: HttpClient, *, text: str, channel: str | None, token: str | None,
                       webhook_url: str | None) -> TestResult:
    """Post with the bot token when there is one, else to the incoming webhook."""
    try:
        if token:
            resp = await http.post("slack", f"{API}/chat.postMessage", headers={"Authorization": f"Bearer {token}"},
                                   json={"channel": channel, "text": text})
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or not body.get("ok"):
                return TestResult(False, f"Slack refused the message: {body.get('error', resp.status_code)}")
            return TestResult(True, f"Posted to {channel}")
        if webhook_url:
            resp = await http.post("slack", webhook_url, json={"text": text})
            ok = resp.status_code == 200
            return TestResult(ok, "Posted through the webhook" if ok else f"Webhook returned {resp.status_code}")
    except (httpx.HTTPError, HttpError) as exc:
        return TestResult(False, redact(f"Could not reach Slack: {type(exc).__name__}", token, webhook_url))
    return TestResult(False, "No SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL is set on the server")


async def test_connection(
    http: HttpClient, *, token: str | None, webhook_url: str | None, channel: str | None
) -> TestResult:
    """Bot token: `auth.test` (posts nothing). Webhook: a webhook cannot be checked without posting, so say so."""
    if token:
        try:
            resp = await http.post("slack", f"{API}/auth.test", headers={"Authorization": f"Bearer {token}"})
            body = resp.json() if resp.content else {}
        except (httpx.HTTPError, HttpError) as exc:
            return TestResult(False, redact(f"Could not reach Slack: {type(exc).__name__}", token))
        if not body.get("ok"):
            return TestResult(False, f"Slack rejected the token: {body.get('error', resp.status_code)}")
        if not channel:
            return TestResult(False, "The token works, but no channel is set")
        return TestResult(True, f"Token valid for {body.get('team', 'the workspace')}; will post to {channel}")
    if webhook_url:
        return TestResult(True, "A webhook URL is set; use Send test message to check it")
    return TestResult(False, "No SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL is set on the server")
