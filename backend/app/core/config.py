"""Environment-driven settings. Read from the process environment and, for local runs, backend/.env.

Empty values (as in .env.example) mean "not set".
"""
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    codebase_pilot_db: str = "codebase_pilot.db"
    codebase_pilot_workspace: str = "workspace"

    npm_registry_url: str = DEFAULT_NPM_REGISTRY
    npm_registry_token: str | None = None
    github_token: str | None = None
    package_sources_path: str | None = None

    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str | None = None
    azure_openai_deployment_strong: str | None = None
    azure_openai_deployment_fast: str | None = None
    azure_openai_api_key: str | None = None
    llm_mode: Literal["live", "record", "replay"] = "live"

    exec_allowed_repos: str = ""
    seeded_logs_dir: str | None = None
    bot_pr_mode: Literal["off", "comment_only", "comment_and_commit"] = "comment_only"
    bot_authors: str = "dependabot[bot],renovate[bot]"
    azure_log_analytics_workspace_id: str | None = None
    azure_log_table: str = "ContainerAppConsoleLogs_CL"
    azure_log_message_column: str = "Log_s"
    azure_log_app_column: str = "ContainerAppName_s"
    log_format: str = ""
    reports_dir: str = "reports"
    checkpoint_db: str = "checkpoints.db"
    pilot_git_author: str = "Codebase Pilot <pilot@localhost>"
    pilot_embedded_worker: bool = False
    pilot_demo_user: str = "demo"
    # Optional shared secret: when set, every write endpoint (scan, decision, feedback, ...) needs
    # `Authorization: Bearer <token>`. The approver is then the `X-Pilot-User` header, not free text.
    pilot_api_token: str | None = None
    # GitHub push webhook (HMAC SHA-256 of the body, header X-Hub-Signature-256); empty = webhook disabled
    github_webhook_secret: str | None = None
    # Re-scan every repository Pilot already knows, this often (0 = never); runs in the embedded worker
    pilot_rescan_minutes: int = 0
    # Post bot-PR comments and the dashboard / team issues on GitHub after every scan (repo_actions job).
    # Off by default: nothing leaves the system without a person asking (ground rule 8). The `bot-prs` and
    # `issues` CLI commands still run them on request.
    pilot_repo_actions: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_unset(cls, value, info):
        # `NAME=` in .env.example must behave like an unset variable (use the default).
        if value == "":
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        return value

    @property
    def exec_allowed(self) -> set[str]:
        return {r.strip() for r in self.exec_allowed_repos.split(",") if r.strip()}


def get_settings() -> Settings:
    """Fresh on every call so tests (and long-running processes) see environment changes."""
    return Settings()
