"""Greeting send log. The unique constraint IS the duplicate-send guarantee."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class OccasionSendLog(Base):
    """One row per (event type, recipient, year).

    PRD section 20 makes duplicate prevention a hard requirement, so it is
    enforced by the database rather than by an application-level check that a
    crash or a concurrent run could race past. The row is inserted and committed
    BEFORE the Gmail send; a crash mid-send therefore leaves a PENDING row that
    blocks a duplicate on the next run rather than allowing one.
    """

    __tablename__ = "occasion_send_log"
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "recipient_email",
            "event_year",
            name="uq_occasion_send_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_year: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(255), default="")
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str] = mapped_column(String(128), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
