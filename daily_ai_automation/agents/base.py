"""The contract every daily worker implements (PRD section 50).

A worker is a bounded job, not a long-running agent: it receives a context,
executes, returns a structured result, and is discarded. The supervisor owns
its lifecycle; the worker owns nothing beyond its own run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ..contracts import AgentResult, AgentStatus
from ..models import utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from ..config.settings import Settings
    from ..repositories import AuditRepository


@dataclass
class RunContext:
    """Everything a worker is allowed to know about the run.

    Workers get exactly this and nothing more (PRD section 7.3: "pass only
    required data/context"). In particular they do not get the supervisor, so
    a worker cannot start another worker.
    """

    run_id: str
    run_date: datetime
    settings: "Settings"
    session: "Session"
    audit: "AuditRepository"
    dry_run: bool = True
    metadata: dict = field(default_factory=dict)


class DailyAgent(ABC):
    """Base class for every worker."""

    #: Stable identifier used in the database, logs and the report.
    name: str = "daily_agent"

    def __init__(self, context: RunContext) -> None:
        self.context = context
        self.settings = context.settings
        self.started_at = utcnow()

    @abstractmethod
    def execute(self) -> AgentResult:
        """Do the work and return a structured result.

        Implementations should catch their own per-item failures and report
        them as warnings/errors with PARTIAL_SUCCESS, and let only genuinely
        run-ending problems propagate so the supervisor can retry them.
        """

    # -- helpers for subclasses -------------------------------------------

    def result(
        self,
        status: AgentStatus,
        *,
        summary: str = "",
        items_processed: int = 0,
        actions_taken: int = 0,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        details: dict | None = None,
    ) -> AgentResult:
        return AgentResult(
            run_id=self.context.run_id,
            agent=self.name,
            status=status,
            started_at=self.started_at,
            completed_at=utcnow(),
            items_processed=items_processed,
            actions_taken=actions_taken,
            errors=errors or [],
            warnings=warnings or [],
            summary=summary,
            details=details or {},
        )

    def audit(
        self,
        action: str,
        *,
        resource_id: str = "",
        outcome: str = "APPLIED",
        reason: str = "",
        confidence: float | None = None,
        details: dict | None = None,
    ) -> None:
        self.context.audit.write(
            run_id=self.context.run_id,
            agent=self.name,
            action=action,
            resource_id=resource_id,
            outcome=outcome,
            dry_run=self.context.dry_run,
            reason=reason,
            confidence=confidence,
            details=details,
        )
