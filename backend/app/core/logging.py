"""Logging setup for CLIs and services. Library code just uses logging.getLogger(__name__)."""
import json
import logging
import os
import sys

# Attributes every LogRecord has; anything else came in through `extra=` (run_id, node, package, ...).
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | int = "WARNING", json_format: bool | None = None) -> None:
    """Log to stderr. JSON lines when json_format is True or LOG_FORMAT=json. Safe to call repeatedly."""
    if json_format is None:
        json_format = os.environ.get("LOG_FORMAT", "").lower() == "json"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_format else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_pilot", False):
            root.removeHandler(existing)
    handler._pilot = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
