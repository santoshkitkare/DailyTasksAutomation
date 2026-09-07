"""A full run through the real supervisor, with only external services faked.

Everything between the CLI and the network is genuine: settings, database,
supervisor, retry, both workers, the policy engine, and report rendering.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from daily_ai_automation.agents.gmail_agent import GmailTriageAgent
from daily_ai_automation.agents.occasion_agent import OccasionAgent
from daily_ai_automation.contracts import (
    EmailCategory,
    EmailClassificationBatch,
    Priority,
    RecommendedAction,
)
from daily_ai_automation.db import session_scope
from daily_ai_automation.models import AuditLog, EmailProcessing, OccasionSendLog
from daily_ai_automation.reporting.delivery import deliver
from daily_ai_automation.repositories import RunRepository
from daily_ai_automation.supervisor.supervisor import AgentRegistration, Supervisor
from tests.conftest import make_classification, make_message
from tests.fakes import FakeDriveClient, FakeGmailClient, FakeImageClient, FakeLLMClient
from tests.test_excel import build_workbook
from tests.test_occasion_agent import greeting

RUN_MOMENT = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def wired(settings, session_factory):
    """A supervisor with both real workers, wired to fake external clients."""
    messages = [
        make_message(message_id="promo", subject="40% off everything"),
        make_message(
            message_id="job",
            sender="Anita <anita@agency.example>",
            subject="Lead Python Engineer opportunity at ABC",
        ),
        make_message(message_id="otp", subject="Your one-time passcode is 481920"),
    ]
    classifications = [
        make_classification(
            message_id="promo", category=EmailCategory.PROMOTION, confidence=0.98
        ),
        make_classification(
            message_id="job",
            category=EmailCategory.JOB_OPPORTUNITY,
            priority=Priority.HIGH,
            requires_reply=True,
            action=RecommendedAction.REPLY,
            suggested_reply="Thanks for reaching out - happy to hear more.",
        ),
        # A confident-but-wrong verdict the safety rules have to catch.
        make_classification(
            message_id="otp", category=EmailCategory.PROMOTION, confidence=0.99
        ),
    ]

    gmail = FakeGmailClient(messages)
    contacts = build_workbook(
        [
            ["Birthday", date(1990, 9, 7), "Rahul Sharma", "rahul@example.com", "", "Friend"],
            ["Birthday", date(1988, 9, 8), "Priya Mehta", "priya@example.com", "", ""],
            ["Birthday", date(1991, 9, 7), "Broken Row", "not-an-email", "", ""],
        ]
    )

    registrations = [
        AgentRegistration(
            name=GmailTriageAgent.name,
            factory=lambda ctx: GmailTriageAgent(
                ctx,
                gmail=gmail,
                llm=FakeLLMClient(
                    responses=[
                        EmailClassificationBatch(classifications=classifications)
                    ]
                ),
            ),
            is_enabled=lambda s: s.gmail.enabled,
        ),
        AgentRegistration(
            name=OccasionAgent.name,
            factory=lambda ctx: OccasionAgent(
                ctx,
                drive=FakeDriveClient(payload=contacts),
                gmail=gmail,
                llm=FakeLLMClient(responses=[greeting()]),
                images=FakeImageClient(),
            ),
            is_enabled=lambda s: s.occasion.enabled,
        ),
    ]
    return Supervisor(settings, session_factory, registrations), gmail, settings


class TestDryRun:
    def test_a_dry_run_changes_nothing_outside_the_database(self, wired, session_factory):
        supervisor, gmail, settings = wired
        assert settings.dry_run is True

        report = supervisor.run(now=RUN_MOMENT)

        assert gmail.labeled == [], "dry run must not label"
        assert gmail.sent == [], "dry run must not send"
        assert report.status == "PARTIAL_SUCCESS"  # one invalid contact row

        with session_scope(session_factory) as session:
            assert session.query(OccasionSendLog).count() == 0
            outcomes = {e.outcome for e in session.query(AuditLog).all()}
            assert outcomes <= {"WOULD_APPLY", "REFUSED"}

    def test_the_dry_run_report_is_written_but_not_emailed(self, wired):
        supervisor, gmail, settings = wired
        report = supervisor.run(now=RUN_MOMENT)

        path, status = deliver(settings, report, gmail=gmail)

        assert path.exists()
        assert "not emailed" in status
        assert gmail.sent == []
        html = path.read_text(encoding="utf-8")
        assert "Dry run" in html
        assert "Lead Python Engineer" in html


class TestLiveRun:
    def test_a_live_run_labels_sends_and_reports(self, wired, session_factory):
        supervisor, gmail, settings = wired
        settings.dry_run = False
        supervisor.settings = settings

        report = supervisor.run(now=RUN_MOMENT)

        # Only the promotional message is labeled: the job email requires a
        # reply and the passcode email trips the sensitive-content rule.
        assert [m for m, _ in gmail.labeled] == ["promo"]

        # One greeting, plus the report itself.
        greetings = [m for m in gmail.sent if "Happy Birthday" in str(m["Subject"])]
        assert len(greetings) == 1
        assert greetings[0]["To"] == "Rahul Sharma <rahul@example.com>"

        path, status = deliver(settings, report, gmail=gmail)
        assert "emailed to" in status
        assert path.exists()

        with session_scope(session_factory) as session:
            assert session.query(EmailProcessing).count() == 3
            sent = session.query(OccasionSendLog).one()
            assert sent.status == "SENT"

    def test_running_twice_repeats_nothing(self, wired, session_factory):
        supervisor, gmail, settings = wired
        settings.dry_run = False
        supervisor.settings = settings

        supervisor.run(now=RUN_MOMENT)
        labeled_after_first = list(gmail.labeled)
        greetings_after_first = [
            m for m in gmail.sent if "Happy Birthday" in str(m["Subject"])
        ]

        supervisor.run(force=True, now=datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc))

        assert gmail.labeled == labeled_after_first, "no message relabeled"
        greetings_after_second = [
            m for m in gmail.sent if "Happy Birthday" in str(m["Subject"])
        ]
        assert len(greetings_after_second) == len(greetings_after_first), (
            "nobody greeted twice"
        )

        with session_scope(session_factory) as session:
            assert session.query(OccasionSendLog).count() == 1


class TestFailureIsolation:
    def test_gmail_failing_does_not_stop_the_greetings(
        self, settings, session_factory
    ):
        """PRD section 32, end to end."""
        settings.dry_run = False
        gmail = FakeGmailClient()
        contacts = build_workbook(
            [["Birthday", date(1990, 9, 7), "Rahul", "rahul@example.com", "", "Friend"]]
        )

        def broken_gmail_agent(ctx):
            failing = FakeGmailClient()
            failing.search_message_ids = _raise(RuntimeError("Gmail API is down"))
            return GmailTriageAgent(ctx, gmail=failing, llm=FakeLLMClient())

        registrations = [
            AgentRegistration(
                name=GmailTriageAgent.name,
                factory=broken_gmail_agent,
                is_enabled=lambda s: True,
            ),
            AgentRegistration(
                name=OccasionAgent.name,
                factory=lambda ctx: OccasionAgent(
                    ctx,
                    drive=FakeDriveClient(payload=contacts),
                    gmail=gmail,
                    llm=FakeLLMClient(responses=[greeting()]),
                    images=FakeImageClient(),
                ),
                is_enabled=lambda s: True,
            ),
        ]

        report = Supervisor(
            settings, session_factory, registrations, sleep=lambda _: None
        ).run(now=RUN_MOMENT)

        assert report.status == "PARTIAL_SUCCESS"
        assert str(report.results["gmail_triage_agent"].status) == "FAILED"
        assert str(report.results["occasion_agent"].status) == "COMPLETED"
        assert len(gmail.sent) == 1, "the greeting still went out"

        rendered_path, _ = deliver(settings, report, gmail=gmail)
        text = rendered_path.read_text(encoding="utf-8")
        assert "Gmail API is down" in text


def _raise(exc):
    def _boom(*args, **kwargs):
        raise exc

    return _boom
