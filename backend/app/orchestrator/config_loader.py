"""Per-repository settings from `.codebase-pilot.yml` (A4). Never raises: bad input becomes defaults + a warning."""
import logging
import shlex
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

CONFIG_NAMES = (".codebase-pilot.yml", ".codebase-pilot.yaml")
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class RepoPilotConfig(BaseModel):
    enabled: bool = True
    mode: Literal["shadow", "advisory", "assisted"] = "assisted"
    service: str | None = None
    owners: str | None = None
    min_severity: Literal["critical", "high", "medium", "low"] = "low"
    min_confidence: float = 0.5
    allowed_paths: list[str] = Field(default_factory=lambda: ["**"])
    test_command: list[str] = Field(default_factory=lambda: ["npm", "test"])

    @field_validator("test_command", mode="before")
    @classmethod
    def _split(cls, value):
        return shlex.split(value) if isinstance(value, str) else value

    def severity_allows(self, severity: str) -> bool:
        """Is a plan of this severity at or above the configured minimum?"""
        return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[self.min_severity]


def _flatten(raw: dict, warnings: list[str] | None = None) -> dict:
    """Accept both flat keys and the nested layout (notify.min_severity, fixes.allowed_paths, fixes.test_command)."""
    flat = {k: v for k, v in raw.items() if k not in ("notify", "fixes")}
    notify, fixes = raw.get("notify") or {}, raw.get("fixes") or {}
    if isinstance(notify, dict):
        flat.update({k: v for k, v in notify.items() if k in ("min_severity", "min_confidence")})
    if isinstance(fixes, dict):
        flat.update({k: v for k, v in fixes.items() if k in ("allowed_paths", "test_command")})
    if flat.get("mode") == "enforce":  # accepted by an earlier version, but it never did anything different
        flat["mode"] = "assisted"
        if warnings is not None:
            warnings.append("mode: enforce is not supported, using assisted (a person still approves every change)")
    return flat


def load_repo_config(repo_root: str | Path | None) -> tuple[RepoPilotConfig, list[str]]:
    """(config, warnings). A missing file gives defaults silently."""
    if repo_root is None:
        return RepoPilotConfig(), []
    path = next((Path(repo_root) / n for n in CONFIG_NAMES if (Path(repo_root) / n).is_file()), None)
    if path is None:
        return RepoPilotConfig(), []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("expected a mapping at the top level")
        warnings: list[str] = []
        return RepoPilotConfig(**_flatten(raw, warnings)), warnings
    except (yaml.YAMLError, ValueError, ValidationError, OSError) as exc:
        message = f"{path.name} ignored, using defaults: {str(exc).splitlines()[0][:200]}"
        logger.warning(message)
        return RepoPilotConfig(), [message]
