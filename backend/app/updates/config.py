"""Step 2 settings. Environment access goes through app.core.config; these are thin wrappers."""
from pathlib import Path

from app.core.config import DEFAULT_NPM_REGISTRY, get_settings

PROMPT_VERSION = "v1"

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
PROMPTS_DIR = Path(__file__).parent / "prompts"
SHARED_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

USER_AGENT = "codebase-pilot/0.1 (hackathon)"
REGISTRY_TTL_HOURS = 6

# Max concurrent requests per host.
HOST_CONCURRENCY = {"npm": 8, "github": 4, "deps_dev": 4, "osv": 4, "slack": 2, "confluence": 2}


def default_db_path() -> str:
    """Same resolution as ingestion's main.py."""
    return get_settings().codebase_pilot_db


def npm_registry_url() -> str:
    """Point at Artifactory / Azure Artifacts in production (same registry API)."""
    return (get_settings().npm_registry_url or DEFAULT_NPM_REGISTRY).rstrip("/")


def npm_registry_token() -> str | None:
    """Bearer token for a private registry. Only ever sent to the NPM_REGISTRY_URL host."""
    return get_settings().npm_registry_token or None
