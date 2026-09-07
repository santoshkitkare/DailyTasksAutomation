"""SQLAlchemy declarative base and shared column helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    All stored timestamps are UTC; the reporter converts to the configured
    local timezone for display only.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass
