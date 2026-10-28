"""Shared async HTTP client: per-host concurrency limits, retries with backoff, Retry-After.

Proxy settings (HTTPS_PROXY) and a custom CA bundle (SSL_CERT_FILE) are honoured by httpx
through the environment. Headers are never logged.
"""
import asyncio
from functools import lru_cache

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.updates.config import HOST_CONCURRENCY, USER_AGENT

MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60.0


@lru_cache(maxsize=1)
def _ssl_context():
    """Built once per process: loading the CA bundle costs ~1.5 s. Honours SSL_CERT_FILE."""
    return httpx.create_ssl_context()


class HttpError(RuntimeError):
    """Retries exhausted on 429 / 5xx."""

    def __init__(self, status_code: int, url: str):
        super().__init__(f"HTTP {status_code} from {url} after {MAX_ATTEMPTS} attempts")
        self.status_code = status_code
        self.url = url


class _RetryableStatus(Exception):
    def __init__(self, status_code: int, retry_after: float | None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER_S))
    except ValueError:  # HTTP-date form: fall back to exponential backoff
        return None


class HttpClient:
    def __init__(self, client: httpx.AsyncClient | None = None, *, wait_scale: float = 1.0):
        """wait_scale multiplies every backoff delay; tests pass 0."""
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=_ssl_context(),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._wait_scale = wait_scale
        self._semaphores = {host: asyncio.Semaphore(n) for host, n in HOST_CONCURRENCY.items()}
        self._backoff = wait_exponential_jitter(initial=1, max=10)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatus) and exc.retry_after is not None:
            return exc.retry_after * self._wait_scale
        return self._backoff(state) * self._wait_scale

    async def request(self, host: str, method: str, url: str, **kwargs) -> httpx.Response:
        """Return the response for any non-retryable status (404 and 304 included).

        Retries timeouts, connection errors, 429 and 5xx; raises HttpError when 429/5xx persists.
        """
        semaphore = self._semaphores.setdefault(host, asyncio.Semaphore(4))
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(MAX_ATTEMPTS),
                wait=self._wait,
                retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
                reraise=True,
            ):
                with attempt:
                    async with semaphore:
                        resp = await self._client.request(method, url, **kwargs)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise _RetryableStatus(resp.status_code, _retry_after(resp))
                    return resp
        except _RetryableStatus as exc:
            raise HttpError(exc.status_code, url) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, host: str, url: str, **kwargs) -> httpx.Response:
        return await self.request(host, "GET", url, **kwargs)

    async def post(self, host: str, url: str, **kwargs) -> httpx.Response:
        return await self.request(host, "POST", url, **kwargs)
