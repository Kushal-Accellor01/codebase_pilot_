"""The one interface every change source implements (CLAUDE.md Part 3.1)."""
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.config import Settings
from app.core.contracts import ChangeEvent, Origin, SourceKind
from app.updates.http import HttpClient


@dataclass
class SourceContext:
    conn: sqlite3.Connection
    http: HttpClient
    settings: Settings


class ChangeSource(Protocol):
    kind: SourceKind
    origin: Origin

    async def poll(self, ctx: SourceContext, repo: str, since: str | None) -> list[ChangeEvent]:
        """Events detected for `repo` since the cursor. Raises only for failures a retry can fix."""

    def from_webhook(self, headers: Mapping[str, str], body: bytes) -> list[ChangeEvent] | None:
        """Events carried by a webhook delivery, or None when this source has no webhook."""
