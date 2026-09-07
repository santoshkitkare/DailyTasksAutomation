"""Structured JSON logging (PRD section 37).

Every record carries run_id/agent/task_id/status/duration/error_code when the
caller supplies them. Email bodies never reach a log record: nothing in this
codebase passes a body to a logger, and the formatter only serialises the
whitelisted context fields below.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

CONTEXT_FIELDS = (
    "run_id",
    "agent",
    "task_id",
    "status",
    "duration",
    "error_code",
    "count",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console output; the JSON file stays machine-readable."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        agent = getattr(record, "agent", None)
        prefix = f"[{agent}] " if agent else ""
        line = f"{stamp} {record.levelname:<7} {prefix}{record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # The Google client is extremely chatty at INFO and leaks URLs with query
    # parameters into the log.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
