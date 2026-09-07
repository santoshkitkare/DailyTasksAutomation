"""Idempotency: the guarantee that running twice is safe (PRD section 42)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from daily_ai_automation.db import session_scope
from daily_ai_automation.models import OccasionSendLog
from daily_ai_automation.repositories import (
    DuplicateSendError,
    EmailRepository,
    OccasionRepository,
    RunRepository,
)
from daily_ai_automation.supervisor.state_manager import DuplicateRunError
from tests.conftest import make_classification

from daily_ai_automation.contracts import TriagedEmail


def _triaged(message_id: str) -> TriagedEmail:
    return TriagedEmail(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="a@example.com",
        subject="Subject",
        received_at=datetime.now(timezone.utc),
        classification=make_classification(message_id=message_id),
    )


class TestGmailDedup:
    def test_a_recorded_message_is_filtered_out_next_run(self, session):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("msg-a"))
        session.commit()

        seen = repo.already_processed(["msg-a", "msg-b"])
        assert seen == {"msg-a"}

    def test_recording_the_same_message_twice_updates_rather_than_duplicates(
        self, session
    ):
        repo = EmailRepository(session)
        repo.record("run-1", _triaged("msg-a"))
        repo.record("run-2", _triaged("msg-a"))
        session.commit()
        assert len(repo.for_run("run-2")) == 1
        assert repo.for_run("run-1") == []

    def test_chunking_handles_more_ids_than_sqlite_allows_per_statement(self, session):
        """SQLite caps bound parameters; a 7-day first run can exceed it."""
        repo = EmailRepository(session)
        many = [f"msg-{i}" for i in range(2500)]
        repo.record("run-1", _triaged("msg-1200"))
        session.commit()
        assert repo.already_processed(many) == {"msg-1200"}


class TestOccasionDedup:
    def _claim(self, repo: OccasionRepository, run_id: str = "run-1"):
        return repo.claim(
            run_id=run_id,
            event_type="Birthday",
            event_date=date(1990, 9, 7),
            event_year=2026,
            recipient_name="Rahul Sharma",
            recipient_email="rahul@example.com",
            content_hash="abc",
        )

    def test_second_claim_for_the_same_identity_is_refused(self, session):
        repo = OccasionRepository(session)
        self._claim(repo)
        with pytest.raises(DuplicateSendError):
            self._claim(repo, run_id="run-2")

    def test_the_database_refuses_a_duplicate_even_without_the_precheck(self, session):
        """The constraint, not the application check, is the real guarantee."""
        repo = OccasionRepository(session)
        self._claim(repo)
        session.add(
            OccasionSendLog(
                run_id="run-2",
                event_type="Birthday",
                event_date=date(1990, 9, 7),
                event_year=2026,
                recipient_name="Rahul Sharma",
                recipient_email="rahul@example.com",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_a_crash_between_claim_and_send_still_blocks_a_duplicate(self, session):
        """The claim is committed before the send, so it survives a crash."""
        repo = OccasionRepository(session)
        claim = self._claim(repo)
        assert claim.status == "PENDING"

        # Simulate the process dying here. A new run finds the pending claim.
        found = repo.find("Birthday", "rahul@example.com", 2026)
        assert found is not None and found.status == "PENDING"

    def test_a_released_claim_can_be_retried_tomorrow(self, session):
        """A failed send must not suppress the greeting for the rest of the year."""
        repo = OccasionRepository(session)
        claim = self._claim(repo)
        repo.release(claim)
        assert repo.find("Birthday", "rahul@example.com", 2026) is None
        self._claim(repo, run_id="run-2")  # succeeds

    def test_the_same_person_is_greeted_again_next_year(self, session):
        repo = OccasionRepository(session)
        self._claim(repo)
        repo.claim(
            run_id="run-2027",
            event_type="Birthday",
            event_date=date(1990, 9, 7),
            event_year=2027,
            recipient_name="Rahul Sharma",
            recipient_email="rahul@example.com",
            content_hash="abc",
        )

    def test_birthday_and_anniversary_are_independent(self, session):
        repo = OccasionRepository(session)
        self._claim(repo)
        repo.claim(
            run_id="run-1",
            event_type="Anniversary",
            event_date=date(2015, 9, 7),
            event_year=2026,
            recipient_name="Rahul Sharma",
            recipient_email="rahul@example.com",
            content_hash="abc",
        )


class TestDailyRunDedup:
    def test_a_second_run_on_the_same_date_is_refused(
        self, settings, session_factory, monkeypatch
    ):
        from daily_ai_automation.supervisor.supervisor import Supervisor

        supervisor = Supervisor(settings, session_factory, [])
        supervisor.run()
        with pytest.raises(DuplicateRunError):
            supervisor.run()

    def test_force_replaces_the_existing_run(self, settings, session_factory):
        from daily_ai_automation.supervisor.supervisor import Supervisor

        tz = settings.scheduler.tzinfo
        supervisor = Supervisor(settings, session_factory, [])
        first = supervisor.run(now=datetime(2026, 9, 7, 9, 0, tzinfo=tz))
        second = supervisor.run(
            force=True, now=datetime(2026, 9, 7, 18, 30, tzinfo=tz)
        )
        assert first.run_id != second.run_id
        assert first.run_date == second.run_date

        with session_scope(session_factory) as session:
            repo = RunRepository(session)
            assert repo.get(first.run_id) is None
            assert repo.get(second.run_id) is not None
