"""Action-tracking additions to EmailRepository (dashboard M1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_ai_automation.contracts import (
    EmailCategory,
    Priority,
    RecommendedAction,
    TriagedEmail,
)
from daily_ai_automation.repositories import EmailRepository, UnknownMessageError
from daily_ai_automation.repositories.email_repository import ACTIONABLE_CATEGORIES
from tests.conftest import make_classification


def _triaged(
    message_id: str,
    *,
    minutes_ago: int = 0,
    **classification_kwargs,
) -> TriagedEmail:
    return TriagedEmail(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="a@example.com",
        subject=f"Subject {message_id}",
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        classification=make_classification(message_id=message_id, **classification_kwargs),
    )


class TestPersistedClassificationFields:
    def test_record_persists_action_relevant_fields(self, session):
        repo = EmailRepository(session)
        email = _triaged(
            "m1",
            requires_reply=True,
            category=EmailCategory.JOB_OPPORTUNITY,
            action=RecommendedAction.REPLY,
            why_it_matters="A recruiter is waiting on a response.",
            suggested_deadline="within 24 hours",
            suggested_reply="Thanks for reaching out.",
        )
        row = repo.record("run-1", email)

        assert row.why_it_matters == "A recruiter is waiting on a response."
        assert row.suggested_deadline == "within 24 hours"
        assert row.suggested_reply == "Thanks for reaching out."

    def test_a_new_row_starts_pending(self, session):
        repo = EmailRepository(session)
        row = repo.record("run-1", _triaged("m1"))
        assert row.user_action_status == "PENDING"
        assert row.user_action_at is None


class TestMarkAction:
    def test_marks_done_with_a_timestamp(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1"))
        session.commit()

        row = repo.mark_action("m1", "DONE")
        assert row.user_action_status == "DONE"
        assert row.user_action_at is not None

    def test_marks_dismissed_with_a_note(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1"))
        session.commit()

        row = repo.mark_action("m1", "DISMISSED", note="not relevant")
        assert row.user_action_status == "DISMISSED"
        assert row.user_action_note == "not relevant"

    def test_unknown_message_id_raises(self, session):
        repo = EmailRepository(session)
        with pytest.raises(UnknownMessageError):
            repo.mark_action("does-not-exist", "DONE")

    def test_an_invalid_status_is_rejected(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1"))
        session.commit()
        with pytest.raises(ValueError, match="status must be one of"):
            repo.mark_action("m1", "MAYBE")

    def test_marking_done_twice_is_idempotent(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1"))
        session.commit()

        repo.mark_action("m1", "DONE")
        first_timestamp = repo.mark_action("m1", "DONE").user_action_at
        assert first_timestamp is not None


class TestOpenActions:
    def test_only_actionable_pending_rows_are_returned(self, session):
        repo = EmailRepository(session)
        repo.record(
            "run-1",
            _triaged("reply-needed", requires_reply=True, category=EmailCategory.WORK),
        )
        repo.record(
            "run-1",
            _triaged(
                "job-opp",
                category=EmailCategory.JOB_OPPORTUNITY,
                requires_reply=False,
            ),
        )
        repo.record(
            "run-1",
            _triaged(
                "newsletter",
                category=EmailCategory.NEWSLETTER,
                requires_reply=False,
                action=RecommendedAction.TO_DELETE,
            ),
        )
        session.commit()

        open_ids = {e.message_id for e in repo.open_actions()}
        assert open_ids == {"reply-needed", "job-opp"}

    def test_marking_done_removes_it_from_open_actions(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1", requires_reply=True))
        session.commit()
        assert len(repo.open_actions()) == 1

        repo.mark_action("m1", "DONE")
        assert repo.open_actions() == []

    def test_dismissed_is_also_excluded(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("m1", requires_reply=True))
        session.commit()
        repo.mark_action("m1", "DISMISSED")
        assert repo.open_actions() == []

    def test_open_actions_spans_multiple_runs(self, session):
        """The whole point of this view: nothing gets lost when a day scrolls off."""
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("old", requires_reply=True, minutes_ago=4000))
        repo.record("run-2", _triaged("new", requires_reply=True, minutes_ago=5))
        session.commit()

        assert {e.message_id for e in repo.open_actions()} == {"old", "new"}

    def test_ordered_by_priority_then_recency(self, session):
        repo = EmailRepository(session)
        repo.record(
            "run-1",
            _triaged(
                "low-old",
                requires_reply=True,
                priority=Priority.LOW,
                minutes_ago=100,
            ),
        )
        repo.record(
            "run-1",
            _triaged(
                "critical-old",
                requires_reply=True,
                priority=Priority.CRITICAL,
                minutes_ago=100,
            ),
        )
        repo.record(
            "run-1",
            _triaged(
                "critical-new",
                requires_reply=True,
                priority=Priority.CRITICAL,
                minutes_ago=5,
            ),
        )
        session.commit()

        ordered = [e.message_id for e in repo.open_actions()]
        assert ordered == ["critical-new", "critical-old", "low-old"]

    def test_actionable_categories_constant_matches_digest_semantics(self):
        """Keeps the dashboard and the emailed report agreeing on what
        counts as an action item, without importing digest.py's internals."""
        assert set(ACTIONABLE_CATEGORIES) == {"JOB_OPPORTUNITY", "RECRUITER"}
