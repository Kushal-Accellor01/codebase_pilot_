"""Read-only GitHub REST client with rate-limit handling (S2 section 8).

Authenticated with GITHUB_TOKEN (Bearer). The token is only ever sent to api.github.com and never logged.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from app.core.config import get_settings
from app.updates.http import HttpClient

logger = logging.getLogger(__name__)

API = "https://api.github.com"
MIN_REMAINING = 50  # pause below this many requests left (authenticated)
MIN_REMAINING_ANONYMOUS = 5  # the anonymous limit is only 60 per hour
MAX_SECONDARY_RETRIES = 2


class GitHubRateLimited(RuntimeError):
    """The limit is exhausted and the reset is too far away to wait for. Remaining work stays 'pending'."""

    def __init__(self, reset_at: float):
        wait = max(0, int(reset_at - time.time()))
        super().__init__(f"GitHub rate limit exhausted; resets in {wait} s")
        self.reset_at = reset_at


class GitHubClient:
    def __init__(
        self,
        http: HttpClient,
        token: str | None = None,
        *,
        base_url: str = API,
        max_wait_s: float = 120.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._token = token if token is not None else get_settings().github_token or None
        self._base = base_url.rstrip("/")
        self._max_wait = max_wait_s
        self._sleep = sleeper
        self._clock = clock
        self._remaining: int | None = None
        self._reset: float = 0.0
        if not self._token:
            logger.warning("GITHUB_TOKEN is not set: GitHub calls are limited to 60 per hour")

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    async def _wait_for_budget(self) -> None:
        floor = MIN_REMAINING if self._token else MIN_REMAINING_ANONYMOUS
        if self._remaining is None or self._remaining >= floor:
            return
        wait = self._reset - self._clock() + 1
        if wait <= 0:
            self._remaining = None  # the window has reset
            return
        if wait > self._max_wait:
            raise GitHubRateLimited(self._reset)
        logger.info("GitHub rate limit low (%s left); pausing %.0f s", self._remaining, wait)
        await self._sleep(wait)
        self._remaining = None

    def _record(self, resp: httpx.Response) -> None:
        remaining, reset = resp.headers.get("x-ratelimit-remaining"), resp.headers.get("x-ratelimit-reset")
        if remaining is not None and remaining.isdigit():
            self._remaining = int(remaining)
        if reset is not None and reset.isdigit():
            self._reset = float(reset)

    async def request(
        self, path: str, *, raw: bool = False, params: dict | None = None, method: str = "GET",
        json: dict | list | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for attempt in range(MAX_SECONDARY_RETRIES + 1):
            await self._wait_for_budget()
            resp = await self._http.request(
                "github", method, f"{self._base}/{path.lstrip('/')}", headers=headers, params=params, json=json
            )
            self._record(resp)
            if resp.status_code not in (403, 429):
                return resp
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit() and attempt < MAX_SECONDARY_RETRIES:
                # Secondary rate limit: wait as told, then retry.
                wait = min(float(retry_after), self._max_wait)
                logger.info("GitHub secondary limit; waiting %.0f s", wait)
                await self._sleep(wait)
                continue
            if self._remaining == 0:
                raise GitHubRateLimited(self._reset)
            return resp  # a real 403 (private repo, blocked): the caller decides
        return resp

    async def get_json(self, path: str, params: dict | None = None):
        """Parsed JSON, or None when the resource doesn't exist (404/410/451) or is forbidden (403)."""
        resp = await self.request(path, params=params)
        if resp.status_code in (403, 404, 410, 451):
            return None
        resp.raise_for_status()
        return resp.json()

    async def get_text(self, path: str, params: dict | None = None) -> str | None:
        resp = await self.request(path, raw=True, params=params)
        if resp.status_code in (403, 404, 410, 451):
            return None
        resp.raise_for_status()
        return resp.text

    async def request_json(self, method: str, path: str, json: dict | list | None = None) -> tuple[int, object]:
        """(status, parsed body or None) for write calls. 4xx are returned, not raised: the caller knows which
        ones are fine (422 'already exists', 404 'no such file yet'). Rate limits still raise."""
        resp = await self.request(path, method=method, json=json)
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = None
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp.status_code, body
