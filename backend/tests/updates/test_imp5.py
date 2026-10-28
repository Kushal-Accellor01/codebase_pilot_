import asyncio
import time

import respx

from app.updates import store
from app.updates.http import HttpClient
from app.updates.registry.batch import fetch_many
from app.updates.registry.npm import NpmRegistry

BASE = "https://registry.npmjs.org"


def doc(name: str) -> dict:
    return {
        "name": name,
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {}},
        "time": {"modified": "2026-01-01T00:00:00.000Z", "1.0.0": "2020-01-01T00:00:00.000Z"},
    }


async def run_batch(conn, names, **kw):
    async with HttpClient(wait_scale=0) as http:
        return await fetch_many(NpmRegistry(http, BASE, token=""), conn, names, **kw)


@respx.mock
def test_twenty_packages_one_failing(patched_db):
    conn = store.connect(str(patched_db))
    store.migrate(conn)
    names = [f"pkg{i}" for i in range(20)]
    for n in names:
        if n == "pkg7":
            respx.get(f"{BASE}/{n}").respond(500)
        else:
            respx.get(f"{BASE}/{n}").respond(200, json=doc(n))

    started = time.perf_counter()
    result = asyncio.run(run_batch(conn, names))
    assert time.perf_counter() - started < 2

    assert len(result.packages) == 19 and list(result.errors) == ["pkg7"]
    assert "HTTP 500" in result.errors["pkg7"]
    rows = {r["name"]: r["status"] for r in conn.execute("SELECT name, status FROM registry_packages")}
    assert len(rows) == 20 and rows["pkg7"] == "error"
    assert sum(1 for s in rows.values() if s == "ok") == 19


@respx.mock
def test_duplicates_fetched_once_and_cache_used(patched_db):
    conn = store.connect(str(patched_db))
    store.migrate(conn)
    route = respx.get(f"{BASE}/a").respond(200, json=doc("a"))
    asyncio.run(run_batch(conn, ["a", "a", "a"]))
    asyncio.run(run_batch(conn, ["a"]))
    assert route.call_count == 1


@respx.mock
def test_exception_without_message_is_described(patched_db):
    import httpx

    conn = store.connect(str(patched_db))
    store.migrate(conn)
    respx.get(f"{BASE}/slow").mock(side_effect=httpx.ReadTimeout(""))
    result = asyncio.run(run_batch(conn, ["slow"]))
    assert result.errors["slow"]  # never empty
