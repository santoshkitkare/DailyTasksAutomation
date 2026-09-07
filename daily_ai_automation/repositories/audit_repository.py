"""Append-only audit trail writer (PRD section 38)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def write(
        self,
        *,
        run_id: str,
        agent: str,
        action: str,
        resource_id: str = "",
        outcome: str = "APPLIED",
        dry_run: bool = False,
        reason: str = "",
        confidence: float | None = None,
        details: dict | None = None,
    ) -> AuditLog:
        """Record one attempted external effect.

        outcome is one of APPLIED (it happened), REFUSED (policy said no),
        WOULD_APPLY (dry run) or FAILED (it was attempted and errored).
        """
        entry = AuditLog(
            run_id=run_id,
            agent=agent,
            action=action,
            resource_id=resource_id,
            outcome=outcome,
            dry_run=dry_run,
            reason=reason,
            confidence=confidence,
            details=details,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def for_run(self, run_id: str) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.run_id == run_id)
            .order_by(AuditLog.timestamp)
        )
        return list(self.session.scalars(stmt))
