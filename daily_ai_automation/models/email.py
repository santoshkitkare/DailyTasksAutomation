"""Per-message triage record. message_id is the Gmail dedup key (PRD 42)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class EmailProcessing(Base):
    """One row per Gmail message ever classified.

    Deliberately stores no body text, only the model summary (PRD section 37).
    The primary key doubles as the idempotency guard: a message present here is
    filtered out before any LLM call on subsequent runs.
    """

    __tablename__ = "email_processing"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    sender: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(32), default="OTHER")
    priority: Mapped[str] = mapped_column(String(16), default="NONE")
    requires_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(String(32), default="NO_ACTION")
    action_applied: Mapped[str] = mapped_column(String(32), default="NONE")
    action_reason: Mapped[str] = mapped_column(Text, default="")
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    # Carried over from EmailClassification so the dashboard's cross-run open
    # actions view can render a card without depending on a run's summary_json
    # blob. Still no full email body - only what the model already summarised.
    why_it_matters: Mapped[str] = mapped_column(Text, default="")
    suggested_deadline: Mapped[str] = mapped_column(String(128), default="")
    suggested_reply: Mapped[str] = mapped_column(Text, default="")

    # User-driven completion tracking for the dashboard (PENDING/DONE/DISMISSED).
    # Deliberately local-only: marking an item done never touches Gmail.
    user_action_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    user_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_action_note: Mapped[str] = mapped_column(Text, default="")
