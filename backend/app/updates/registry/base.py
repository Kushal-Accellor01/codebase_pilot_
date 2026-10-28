from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

PackageStatus = Literal["ok", "not_found", "not_modified"]


@dataclass
class VersionInfo:
    version: str
    published_at: str | None = None  # ISO-8601 as served by the registry
    is_prerelease: bool = False
    deprecation_message: str | None = None
    engines: dict[str, str] = field(default_factory=dict)
    peer_dependencies: dict[str, str] = field(default_factory=dict)


@dataclass
class PackageInfo:
    ecosystem: str
    name: str
    status: PackageStatus = "ok"
    latest_version: str | None = None
    versions: list[VersionInfo] = field(default_factory=list)
    repository: str | dict[str, Any] | None = None  # raw registry value; see sources.parse_github_repo
    homepage: str | None = None
    registry_modified: str | None = None
    etag: str | None = None
    is_deprecated: bool = False  # the latest version is deprecated
    deprecation_message: str | None = None


class RegistryAuthError(RuntimeError):
    """The registry answered 401/403. Not retried; the message never contains the token."""


class RegistryAdapter(Protocol):
    ecosystem: str

    async def fetch_package(self, name: str, *, etag: str | None = None) -> PackageInfo: ...
