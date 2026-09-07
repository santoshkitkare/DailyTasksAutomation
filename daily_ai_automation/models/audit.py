"""Audit trail for every external write attempt (PRD section 38)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class AuditLog(Base):
    """Records what was done, what was refused, and what a dry run WOULD do.

    Refusals are recorded as deliberately as successes: the reason a message was
    left alone is the evidence that the policy engine is working.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="APPLIED")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
