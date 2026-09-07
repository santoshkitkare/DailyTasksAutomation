"""Reads and writes for email_processing (Gmail idempotency)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import TriagedEmail
from ..models import EmailProcessing


class EmailRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def already_processed(self, message_ids: Iterable[str]) -> set[str]:
        """Which of these message IDs have been classified before.

        Chunked because SQLite caps the number of bound parameters per
        statement, and a first run with a 7-day window can exceed it.
        """
        ids = list(message_ids)
        seen: set[str] = set()
        chunk = 500
        for start in range(0, len(ids), chunk):
            batch = ids[start : start + chunk]
            stmt = select(EmailProcessing.message_id).where(
                EmailProcessing.message_id.in_(batch)
            )
            seen.update(self.session.scalars(stmt))
        return seen

    def record(self, run_id: str, email: TriagedEmail) -> EmailProcessing:
        classification = email.classification
        row = self.session.get(EmailProcessing, email.message_id)
        if row is None:
            row = EmailProcessing(message_id=email.message_id)
            self.session.add(row)
        row.thread_id = email.thread_id
        row.run_id = run_id
        row.sender = email.sender[:320]
        row.subject = email.subject
        row.received_at = email.received_at
        row.category = str(classification.category)
        row.priority = str(classification.priority)
        row.requires_reply = classification.requires_reply
        row.confidence = classification.confidence
        row.summary = classification.summary
        row.recommended_action = str(classification.recommended_action)
        row.action_applied = email.action_applied
        row.action_reason = email.action_reason
        self.session.flush()
        return row

    def record_many(self, run_id: str, emails: Sequence[TriagedEmail]) -> int:
        for email in emails:
            self.record(run_id, email)
        return len(emails)

    def for_run(self, run_id: str) -> list[EmailProcessing]:
        stmt = select(EmailProcessing).where(EmailProcessing.run_id == run_id)
        return list(self.session.scalars(stmt))
