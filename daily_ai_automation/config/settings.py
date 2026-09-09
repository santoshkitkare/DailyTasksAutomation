"""Typed configuration loaded from config.yaml plus secrets from .env.

Everything the application reads at runtime flows through `Settings`. Nothing
else in the codebase reads os.environ or parses YAML, so a missing or malformed
value fails once, loudly, at startup rather than halfway through a run.
"""

from __future__ import annotations

import os
from datetime import time as dt_time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class SchedulerSettings(BaseModel):
    timezone: str = "Asia/Kolkata"
    daily_run_time: str = "09:00"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - env dependent
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("daily_run_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            hour, minute = (int(part) for part in v.split(":"))
            dt_time(hour, minute)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"daily_run_time must be HH:MM, got {v!r}") from exc
        return v

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class DatabaseSettings(BaseModel):
    url: str = "sqlite:///data/automation.db"


class GoogleSettings(BaseModel):
    credentials_file: str = "credentials.json"
    token_file: str = "data/token.json"

    def credentials_path(self, root: Path) -> Path:
        return _resolve(root, self.credentials_file)

    def token_path(self, root: Path) -> Path:
        return _resolve(root, self.token_file)


class GmailSettings(BaseModel):
    enabled: bool = True
    lookback_minutes: int = Field(default=1440, gt=0)
    first_run_lookback_days: int = Field(default=7, gt=0)
    max_emails_per_run: int = Field(default=200, gt=0)
    delete_label: str = "ToDelete"
    auto_label_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    auto_label_allowed_categories: list[str] = Field(
        default_factory=lambda: ["NEWSLETTER", "PROMOTION", "SOCIAL"]
    )
    protected_senders: list[str] = Field(default_factory=list)
    base_query: str = "-in:spam -in:trash"

    @field_validator("auto_label_allowed_categories")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c and c.strip()]

    @field_validator("protected_senders")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [s.strip().lower() for s in v if s and s.strip()]


class OccasionSettings(BaseModel):
    enabled: bool = True
    drive_file_id: str = ""
    worksheet: str = "Contacts"
    send_enabled: bool = True
    birthday_enabled: bool = True
    anniversary_enabled: bool = True
    sender_name: str = ""


class NotificationSettings(BaseModel):
    enabled: bool = True
    channel: str = "email"
    recipient: str = ""

    @field_validator("channel")
    @classmethod
    def _supported(cls, v: str) -> str:
        if v != "email":
            raise ValueError("only the 'email' notification channel is implemented")
        return v


class AISettings(BaseModel):
    classification_model: str = "claude-opus-5"
    content_model: str = "claude-opus-5"
    image_model: str = "gemini-2.5-flash-image"
    classification_batch_size: int = Field(default=10, gt=0)
    max_body_chars: int = Field(default=4000, gt=0)
    image_aspect_ratio: str = "1:1"
    image_size: str = "1K"


class RetrySettings(BaseModel):
    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: list[int] = Field(default_factory=lambda: [30, 120, 300])
    agent_timeout_seconds: int = Field(default=1800, gt=0)

    @model_validator(mode="after")
    def _enough_backoff(self) -> "RetrySettings":
        if len(self.backoff_seconds) < self.max_attempts - 1:
            raise ValueError(
                "backoff_seconds must supply a delay for every retry after the "
                f"first attempt (need {self.max_attempts - 1}, "
                f"got {len(self.backoff_seconds)})"
            )
        return self


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: str = "data/logs/automation.jsonl"


class Secrets(BaseModel):
    """Values read from the environment, never from config.yaml."""

    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    token_encryption_key: str = ""


class Settings(BaseModel):
    dry_run: bool = True
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    occasion: OccasionSettings = Field(default_factory=OccasionSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    ai: AISettings = Field(default_factory=AISettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    secrets: Secrets = Field(default_factory=Secrets)
    project_root: Path = PROJECT_ROOT

    # -- derived paths -----------------------------------------------------

    @property
    def database_url(self) -> str:
        """Absolute-ise a relative SQLite path so the CWD can't move the DB."""
        prefix = "sqlite:///"
        if self.database.url.startswith(prefix):
            raw = self.database.url[len(prefix) :]
            path = _resolve(self.project_root, raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path.as_posix()}"
        return self.database.url

    @property
    def log_path(self) -> Path:
        return _resolve(self.project_root, self.logging.file)

    @property
    def reports_dir(self) -> Path:
        return self.project_root / "data" / "reports"

    @property
    def images_dir(self) -> Path:
        return self.project_root / "data" / "images"


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_settings(
    config_path: Path | None = None, *, project_root: Path | None = None
) -> Settings:
    """Read config.yaml + .env into a validated Settings object."""
    root = project_root or PROJECT_ROOT
    path = config_path or (root / "config.yaml")

    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {path}. Copy the one from the repository root."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")

    load_dotenv(root / ".env", override=False)
    _apply_env_overrides(raw)

    raw["secrets"] = {
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "token_encryption_key": os.getenv("TOKEN_ENCRYPTION_KEY", ""),
    }
    raw["project_root"] = root
    return Settings.model_validate(raw)


#: Operator-specific settings, overridable from .env so config.yaml can stay a
#: committable template. Each entry maps an environment variable to the nested
#: config path it replaces, plus how to parse it.
#: (env var, (section, key), parser)
ENV_OVERRIDES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("DAILY_DRY_RUN", ("dry_run",), "bool"),
    ("DAILY_DRIVE_FILE_ID", ("occasion", "drive_file_id"), "str"),
    ("DAILY_SENDER_NAME", ("occasion", "sender_name"), "str"),
    ("DAILY_NOTIFY_RECIPIENT", ("notifications", "recipient"), "str"),
    ("DAILY_PROTECTED_SENDERS", ("gmail", "protected_senders"), "csv"),
    ("DAILY_TIMEZONE", ("scheduler", "timezone"), "str"),
    ("DAILY_DATABASE_URL", ("database", "url"), "str"),
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _parse_env_value(name: str, value: str, kind: str):
    if kind == "bool":
        normalised = value.strip().casefold()
        if normalised in _TRUE:
            return True
        if normalised in _FALSE:
            return False
        raise ValueError(
            f"{name}={value!r} is not a boolean. Use true or false."
        )
    if kind == "csv":
        return [part.strip() for part in value.split(",") if part.strip()]
    return value.strip()


def _apply_env_overrides(raw: dict) -> None:
    """Overlay operator-specific values from the environment onto the YAML.

    This exists so config.yaml can be committed to a public repository without
    carrying anyone's Drive file ID, real name, or protected-sender addresses.
    An unset variable changes nothing, so the YAML remains the single source of
    defaults; .env only ever overrides.

    Note that an explicitly empty value (DAILY_SENDER_NAME=) is treated as unset
    rather than as "override with empty" - blanking a required field from the
    environment is far more likely to be an editing accident than an intent.
    """
    for name, path, kind in ENV_OVERRIDES:
        value = os.getenv(name)
        if value is None or not value.strip():
            continue

        parsed = _parse_env_value(name, value, kind)
        target = raw
        for part in path[:-1]:
            existing = target.get(part)
            if not isinstance(existing, dict):
                existing = {}
                target[part] = existing
            target = existing
        target[path[-1]] = parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
