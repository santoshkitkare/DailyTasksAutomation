"""Reads and writes for occasion_send_log (duplicate-send prevention)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import OccasionSendLog, utcnow


class DuplicateSendError(RuntimeError):
    """Raised when the (event_type, email, year) identity already exists."""


class OccasionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find(
        self, event_type: str, recipient_email: str, event_year: int
    ) -> OccasionSendLog | None:
        stmt = select(OccasionSendLog).where(
            OccasionSendLog.event_type == event_type,
            OccasionSendLog.recipient_email == recipient_email,
            OccasionSendLog.event_year == event_year,
        )
        return self.session.scalars(stmt).first()

    def claim(
        self,
        *,
        run_id: str,
        event_type: str,
        event_date: date,
        event_year: int,
        recipient_name: str,
        recipient_email: str,
        content_hash: str,
    ) -> OccasionSendLog:
        """Reserve the right to send this greeting, or refuse.

        Committed immediately so the claim survives a crash during the send.
        The database unique constraint is what actually prevents the duplicate;
        the pre-check below only produces a nicer error in the common case.
        """
        existing = self.find(event_type, recipient_email, event_year)
        if existing is not None:
            raise DuplicateSendError(
                f"{event_type} greeting for {recipient_email} in {event_year} "
                f"already logged with status {existing.status}"
            )

        row = OccasionSendLog(
            run_id=run_id,
            event_type=event_type,
            event_date=event_date,
            event_year=event_year,
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            content_hash=content_hash,
            status="PENDING",
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise DuplicateSendError(
                f"{event_type} greeting for {recipient_email} in {event_year} "
                "was claimed concurrently"
            ) from exc
        return row

    def mark_sent(self, row: OccasionSendLog, provider_message_id: str) -> None:
        row.status = "SENT"
        row.sent_at = utcnow()
        row.provider_message_id = provider_message_id
        self.session.commit()

    def release(self, row: OccasionSendLog) -> None:
        """Give back a claim that never resulted in a send.

        The row is deleted rather than marked FAILED so tomorrow's run can
        retry: a permanent FAILED row would silently suppress the greeting for
        the rest of the year. The failure itself is recorded in audit_log by
        the caller.
        """
        self.session.delete(row)
        self.session.commit()

    def for_run(self, run_id: str) -> list[OccasionSendLog]:
        stmt = select(OccasionSendLog).where(OccasionSendLog.run_id == run_id)
        return list(self.session.scalars(stmt))
