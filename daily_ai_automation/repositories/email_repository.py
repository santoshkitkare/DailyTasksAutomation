"""Reads and writes for email_processing (Gmail idempotency)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..contracts import TriagedEmail
from ..models import EmailProcessing, utcnow

#: Same predicate as reporting/digest.py's Action Required section
#: (`_action_items`), so the dashboard's "open actions" list and the emailed
#: digest never disagree about what counts as actionable.
ACTIONABLE_CATEGORIES = ("JOB_OPPORTUNITY", "RECRUITER")

USER_ACTION_STATUSES = ("PENDING", "DONE", "DISMISSED")


class UnknownMessageError(LookupError):
    """No email_processing row exists for the given message_id."""


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
        row.why_it_matters = classification.why_it_matters
        row.suggested_deadline = classification.suggested_deadline
        row.suggested_reply = classification.suggested_reply
        self.session.flush()
        return row

    def record_many(self, run_id: str, emails: Sequence[TriagedEmail]) -> int:
        for email in emails:
            self.record(run_id, email)
        return len(emails)

    def for_run(self, run_id: str) -> list[EmailProcessing]:
        stmt = select(EmailProcessing).where(EmailProcessing.run_id == run_id)
        return list(self.session.scalars(stmt))

    # -- action tracking (dashboard) ---------------------------------------

    def open_actions(self, limit: int = 200) -> list[EmailProcessing]:
        """Actionable emails across every run that are still PENDING.

        Cross-run by design: a reply-required email from three days ago must
        not disappear just because a later day's report has since been read.
        Ordered most urgent first (CRITICAL/HIGH priority, most recent).
        """
        priority_rank = {
            "CRITICAL": 0,
            "HIGH": 1,
            "MEDIUM": 2,
            "LOW": 3,
            "NONE": 4,
        }
        stmt = select(EmailProcessing).where(
            EmailProcessing.user_action_status == "PENDING",
            (EmailProcessing.requires_reply.is_(True))
            | (EmailProcessing.category.in_(ACTIONABLE_CATEGORIES)),
        )
        rows = list(self.session.scalars(stmt))
        rows.sort(
            key=lambda r: (
                priority_rank.get(r.priority, 9),
                -(r.received_at.timestamp() if r.received_at else 0),
            )
        )
        return rows[:limit]

    def mark_action(
        self, message_id: str, status: str, *, note: str = ""
    ) -> EmailProcessing:
        """Record that the user has handled (or dismissed) an action item.

        Purely local bookkeeping - this never writes to Gmail. Sending or
        drafting a reply from the dashboard is a separate, larger decision
        that reopens the human-in-the-loop boundary and is out of scope here.
        """
        if status not in USER_ACTION_STATUSES:
            raise ValueError(
                f"status must be one of {USER_ACTION_STATUSES}, got {status!r}"
            )
        row = self.session.get(EmailProcessing, message_id)
        if row is None:
            raise UnknownMessageError(
                f"No email_processing row for message_id={message_id!r}"
            )
        row.user_action_status = status
        row.user_action_at = utcnow()
        row.user_action_note = note
        self.session.flush()
        return row
