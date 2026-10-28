import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from app.updates import store
from app.updates.config import REGISTRY_TTL_HOURS
from app.updates.registry.base import PackageInfo, RegistryAdapter
from app.updates.registry.cache import get_package

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    packages: dict[str, PackageInfo] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _describe(exc: Exception) -> str:
    return str(exc) or type(exc).__name__  # e.g. httpx.ReadTimeout has an empty message


async def fetch_many(
    adapter: RegistryAdapter,
    conn: sqlite3.Connection,
    names: list[str],
    *,
    refresh: bool = False,
    ttl_hours: float = REGISTRY_TTL_HOURS,
) -> BatchResult:
    """Fetch many packages concurrently (the HTTP client's per-host semaphore bounds the fan-out).

    Each result is saved as it completes. A failure becomes an 'error' row in registry_packages and an
    entry in BatchResult.errors; it never cancels the other fetches.
    """
    result = BatchResult()

    async def one(name: str) -> None:
        started = time.perf_counter()
        try:
            result.packages[name] = await get_package(adapter, conn, name, ttl_hours=ttl_hours, refresh=refresh)
            outcome = result.packages[name].status
        except Exception as exc:  # one package never stops the batch
            message = _describe(exc)
            result.errors[name] = message
            store.save_registry_failure(conn, adapter.ecosystem, name, message)
            outcome = f"error: {message}"
        logger.info(
            "registry %s %s (%d ms)", name, outcome, (time.perf_counter() - started) * 1000,
            extra={"package": name},
        )

    await asyncio.gather(*(one(n) for n in dict.fromkeys(names)))
    return result
