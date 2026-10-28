from typing import Any
from urllib.parse import quote

from app.updates.config import npm_registry_url
from app.updates.http import HttpClient
from app.updates.registry.base import PackageInfo, VersionInfo

# 'created' and 'modified' live next to the version keys in the registry's `time` map.
_TIME_META_KEYS = {"created", "modified"}


def package_url(base_url: str, name: str) -> str:
    """'@scope/pkg' -> '<base>/@scope%2Fpkg'."""
    return f"{base_url.rstrip('/')}/{quote(name, safe='@')}"


def _deprecation(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def parse_package(name: str, doc: dict[str, Any], etag: str | None = None) -> PackageInfo:
    """Parse the FULL registry document (not the abbreviated install-v1 form, which drops `time`)."""
    times: dict[str, str] = doc.get("time") or {}
    raw_versions: dict[str, Any] = doc.get("versions") or {}
    latest = (doc.get("dist-tags") or {}).get("latest")

    versions = []
    for version, meta in raw_versions.items():
        meta = meta or {}
        versions.append(
            VersionInfo(
                version=version,
                published_at=None if version in _TIME_META_KEYS else times.get(version),
                is_prerelease="-" in version,
                deprecation_message=_deprecation(meta.get("deprecated")),
                engines=meta.get("engines") if isinstance(meta.get("engines"), dict) else {},
                peer_dependencies=meta.get("peerDependencies")
                if isinstance(meta.get("peerDependencies"), dict)
                else {},
            )
        )

    latest_meta = raw_versions.get(latest) or {} if latest else {}
    latest_deprecation = _deprecation(latest_meta.get("deprecated"))
    return PackageInfo(
        ecosystem="npm",
        name=name,
        latest_version=latest,
        versions=versions,
        repository=latest_meta.get("repository") or doc.get("repository"),
        homepage=latest_meta.get("homepage") or doc.get("homepage"),
        registry_modified=times.get("modified"),
        etag=etag,
        is_deprecated=latest_deprecation is not None,
        deprecation_message=latest_deprecation,
    )


class NpmRegistry:
    ecosystem = "npm"

    def __init__(self, http: HttpClient, base_url: str | None = None):
        self._http = http
        self._base_url = base_url or npm_registry_url()

    async def fetch_package(self, name: str, *, etag: str | None = None) -> PackageInfo:
        headers = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        resp = await self._http.get("npm", package_url(self._base_url, name), headers=headers)
        if resp.status_code == 404:
            return PackageInfo(ecosystem="npm", name=name, status="not_found")
        if resp.status_code == 304:
            return PackageInfo(ecosystem="npm", name=name, status="not_modified", etag=etag)
        resp.raise_for_status()
        return parse_package(name, resp.json(), etag=resp.headers.get("etag"))
