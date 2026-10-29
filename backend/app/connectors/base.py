"""Shared pieces of the connectors (S6): where secrets come from, and what a connection test returns.

A channel stores the NAME of a server secret. Values are read here, from the process environment, and only for names
the allowlist accepts (`app.channels.store.valid_secret_ref`). They never reach the browser, the database or a log.
"""
import os
from dataclasses import dataclass

from app.channels.store import SECRET_PREFIX, valid_secret_ref
from app.core.config import get_settings


@dataclass
class TestResult:
    __test__ = False  # not a pytest class
    ok: bool
    detail: str


def secret(name: str | None) -> str | None:
    """The value of an allowlisted server secret, or None when the name is not allowed or not set."""
    if not name or not valid_secret_ref(name):
        return None
    if name.startswith(SECRET_PREFIX):
        return os.environ.get(name) or None
    value = getattr(get_settings(), name.lower(), None)  # the fixed names are Settings fields (so `.env` works)
    return str(value) if value else None


def redact(text: str, *values: str | None) -> str:
    """Remove secret values from a message before it is stored or shown."""
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text
