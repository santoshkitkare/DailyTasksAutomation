"""Supervisor run and per-agent execution records (PRD section 41)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class AutomationRun(Base):
    """One daily supervisor execution.

    run_date is unique: this is PRD section 42 guard against a second run on
    the same day silently duplicating work. The --force flag on the CLI deletes
    the existing row and recreates it.
    """

    __tablename__ = "automation_run"
    __table_args__ = (UniqueConstraint("run_date", name="uq_automation_run_date"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="STARTED")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    summary: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[dict | None] = mapped_column(JSON)

    executions: Mapped[list["AgentExecution"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AgentExecution(Base):
    """One worker attempt within a run."""

    __tablename__ = "agent_execution"
    __table_args__ = (
        UniqueConstraint("run_id", "agent_name", name="uq_agent_execution_run_agent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("automation_run.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict | None] = mapped_column(JSON)

    run: Mapped[AutomationRun] = relationship(back_populates="executions")
