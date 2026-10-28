import sqlite3
from datetime import datetime, timedelta, timezone

from app.updates import store
from app.updates.config import REGISTRY_TTL_HOURS
from app.updates.registry.base import PackageInfo, RegistryAdapter


async def get_package(
    adapter: RegistryAdapter,
    conn: sqlite3.Connection,
    name: str,
    *,
    ttl_hours: float = REGISTRY_TTL_HOURS,
    refresh: bool = False,
) -> PackageInfo:
    """Registry lookup with a TTL cache. The network call happens outside any DB transaction.

    Fresh cache -> no request. Stale cache -> conditional request with the stored etag;
    304 keeps the cached data and only bumps fetched_at.
    """
    cached = store.load_registry_package(conn, adapter.ecosystem, name)
    if cached and not refresh:
        info, fetched_at = cached
        if info.status in ("ok", "not_found") and datetime.now(timezone.utc) - fetched_at < timedelta(
            hours=ttl_hours
        ):
            return info

    etag = cached[0].etag if cached and cached[0].status == "ok" else None
    fresh = await adapter.fetch_package(name, etag=etag)
    if fresh.status == "not_modified" and cached:
        store.save_registry_package(conn, cached[0])  # same data, new fetched_at
        return cached[0]
    store.save_registry_package(conn, fresh)
    return fresh
