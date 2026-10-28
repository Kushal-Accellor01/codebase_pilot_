import os
from pathlib import Path

PROMPT_VERSION = "v1"

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

USER_AGENT = os.environ.get("PILOT_USER_AGENT", "codebase-pilot/0.1 (hackathon)")
REGISTRY_TTL_HOURS = 6

# Max concurrent requests per host.
HOST_CONCURRENCY = {"npm": 8, "github": 4, "deps_dev": 4, "osv": 1}


def default_db_path() -> str:
    """Same resolution as ingestion's main.py."""
    return os.environ.get("CODEBASE_PILOT_DB", "codebase_pilot.db")


def npm_registry_url() -> str:
    """Point at Artifactory / Azure Artifacts in production (same registry API)."""
    return os.environ.get("NPM_REGISTRY_URL", "https://registry.npmjs.org").rstrip("/")


def npm_registry_token() -> str | None:
    """Bearer token for a private registry. Only ever sent to the NPM_REGISTRY_URL host."""
    return os.environ.get("NPM_REGISTRY_TOKEN") or None
