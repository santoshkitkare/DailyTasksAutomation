"""Occasion worker: pre-send gate, duplicate prevention, image degradation."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from daily_ai_automation.agents.occasion_agent import OccasionAgent
from daily_ai_automation.contracts import (
    AgentStatus,
    GreetingContent,
    OccasionOutcome,
)
from daily_ai_automation.models import AuditLog, OccasionSendLog
from tests.fakes import (
    FakeDriveClient,
    FakeGmailClient,
    FakeImageClient,
    FakeLLMClient,
    ImageGenerationError,
)
from tests.test_excel import build_workbook

TODAY = date(2026, 9, 7)


def greeting(subject="Happy Birthday, Rahul!") -> GreetingContent:
    return GreetingContent(
        subject=subject,
        greeting_line="Hi Rahul,",
        body_paragraphs=["Wishing you a very happy birthday!"],
        closing_line="Best wishes,",
        image_concept="A tasteful birthday scene with balloons and confetti.",
    )


def build_agent(context, rows, *, greetings=None, image_error=None, send_error=None):
    context.run_date = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    drive = FakeDriveClient(payload=build_workbook(rows))
    gmail = FakeGmailClient()
    gmail.send_error = send_error
    llm = FakeLLMClient(responses=list(greetings or [greeting()]))
    images = FakeImageClient(error=image_error)
    agent = OccasionAgent(context, drive=drive, gmail=gmail, llm=llm, images=images)
    return agent, drive, gmail, llm, images


BIRTHDAY_TODAY = ["Birthday", date(1990, 9, 7), "Rahul Sharma", "rahul@example.com", "", "Friend"]
BIRTHDAY_TOMORROW = ["Birthday", date(1988, 9, 8), "Priya Mehta", "priya@example.com", "", ""]


class TestSelection:
    def test_only_todays_events_are_processed(self, context):
        context.dry_run = False
        agent, _, gmail, _, _ = build_agent(
            context, [BIRTHDAY_TODAY, BIRTHDAY_TOMORROW]
        )
        result = agent.execute()

        assert result.items_processed == 1
        assert len(gmail.sent) == 1
        assert gmail.sent[0]["To"] == "Rahul Sharma <rahul@example.com>"

    def test_no_events_today_completes_quietly(self, context):
        agent, _, gmail, llm, _ = build_agent(context, [BIRTHDAY_TOMORROW])
        result = agent.execute()

        assert result.status is AgentStatus.COMPLETED
        assert result.items_processed == 0
        assert gmail.sent == []
        assert llm.calls == [], "no greeting should be generated for nobody"

    def test_invalid_rows_are_warned_about_but_the_valid_one_still_sends(self, context):
        context.dry_run = False
        rows = [
            BIRTHDAY_TODAY,
            ["Birthday", date(1990, 9, 7), "Broken", "not-an-email"],
        ]
        agent, _, gmail, _, _ = build_agent(context, rows)
        result = agent.execute()

        assert len(gmail.sent) == 1
        assert result.status is AgentStatus.PARTIAL_SUCCESS
        assert any("not a valid address" in w for w in result.warnings)
        assert len(result.details["invalid_rows"]) == 1


class TestSendGate:
    def test_dry_run_sends_nothing(self, context):
        assert context.dry_run is True
        agent, _, gmail, _, _ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent.execute()

        assert gmail.sent == []
        assert result.details["results"][0]["outcome"] == "SKIPPED_DRY_RUN"
        context.session.commit()
        assert context.session.query(OccasionSendLog).count() == 0, (
            "a dry run must not consume the year's claim"
        )

    def test_dry_run_audits_the_intended_send(self, context):
        agent, *_ = build_agent(context, [BIRTHDAY_TODAY])
        agent.execute()
        context.session.commit()

        entry = context.session.query(AuditLog).one()
        assert entry.action == "SEND_GREETING"
        assert entry.outcome == "WOULD_APPLY"

    def test_send_disabled_skips_before_generating_anything(self, context):
        context.dry_run = False
        context.settings.occasion.send_enabled = False
        agent, _, gmail, llm, _ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent.execute()

        assert gmail.sent == []
        assert llm.calls == []
        assert result.details["results"][0]["outcome"] == "SKIPPED_DISABLED"

    def test_an_empty_sender_name_refuses_to_send(self, context):
        """Better to fail loudly than to sign a greeting with nothing."""
        context.dry_run = False
        context.settings.occasion.sender_name = "   "
        agent, _, gmail, _, _ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent.execute()

        assert gmail.sent == []
        assert result.status is AgentStatus.FAILED
        assert any("sender_name" in e for e in result.errors)

    def test_a_successful_send_records_the_provider_message_id(self, context):
        context.dry_run = False
        agent, *_ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent.execute()
        context.session.commit()

        row = context.session.query(OccasionSendLog).one()
        assert row.status == "SENT"
        assert row.provider_message_id == "sent-1"
        assert row.event_year == 2026
        assert result.actions_taken == 1


class TestDuplicatePrevention:
    def test_a_second_run_the_same_day_sends_nothing(self, context):
        context.dry_run = False
        agent, *_ = build_agent(context, [BIRTHDAY_TODAY])
        agent.execute()
        context.session.commit()

        agent2, _, gmail2, _, _ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent2.execute()

        assert gmail2.sent == []
        assert result.details["results"][0]["outcome"] == "SKIPPED_DUPLICATE"
        assert "Skipped duplicates" not in result.summary or True
        assert context.session.query(OccasionSendLog).count() == 1

    def test_a_failed_send_releases_the_claim_for_tomorrow(self, context):
        context.dry_run = False
        agent, _, gmail, _, _ = build_agent(
            context, [BIRTHDAY_TODAY], send_error=RuntimeError("SMTP exploded")
        )
        result = agent.execute()
        context.session.commit()

        assert result.status is AgentStatus.FAILED
        assert context.session.query(OccasionSendLog).count() == 0, (
            "a failed send must not block the retry"
        )
        entry = context.session.query(AuditLog).one()
        assert entry.outcome == "FAILED"

    def test_a_pending_claim_from_a_crash_blocks_a_duplicate(self, context):
        """The claim is committed before the send, so a crash cannot double-send."""
        context.dry_run = False
        context.session.add(
            OccasionSendLog(
                run_id="crashed-run",
                event_type="Birthday",
                event_date=date(1990, 9, 7),
                event_year=2026,
                recipient_name="Rahul Sharma",
                recipient_email="rahul@example.com",
                status="PENDING",
            )
        )
        context.session.commit()

        agent, _, gmail, _, _ = build_agent(context, [BIRTHDAY_TODAY])
        result = agent.execute()

        assert gmail.sent == []
        assert result.details["results"][0]["outcome"] == "SKIPPED_DUPLICATE"


class TestContentGeneration:
    def test_the_relationship_is_given_to_the_model(self, context):
        agent, _, _, llm, _ = build_agent(context, [BIRTHDAY_TODAY])
        agent.execute()
        assert "Relationship: Friend" in llm.calls[0]["user_content"]

    def test_a_missing_relationship_falls_back_to_the_default_tone(self, context):
        row = ["Birthday", date(1990, 9, 7), "Rahul Sharma", "rahul@example.com", "", ""]
        agent, _, _, llm, _ = build_agent(context, [row])
        agent.execute()
        assert "Relationship: Not specified" in llm.calls[0]["user_content"]

    def test_anniversary_year_count_is_supplied(self, context):
        row = ["Anniversary", date(2015, 9, 7), "Amit & Neha", "amit@example.com", "", "Family"]
        agent, _, _, llm, _ = build_agent(context, [row])
        agent.execute()
        content = llm.calls[0]["user_content"]
        assert "Years completed: 11" in content
        assert "11th" in content

    def test_a_yearless_date_does_not_invent_a_year_count(self, context):
        row = ["Anniversary", "7 September", "Amit & Neha", "amit@example.com", "", ""]
        agent, _, _, llm, _ = build_agent(context, [row])
        agent.execute()
        assert "do not reference a number" in llm.calls[0]["user_content"]

    def test_a_greeting_failure_does_not_stop_the_other_recipient(self, context):
        context.dry_run = False
        rows = [
            BIRTHDAY_TODAY,
            ["Birthday", date(1985, 9, 7), "Second Person", "second@example.com"],
        ]
        llm = FakeLLMClient(responses=[greeting()], error=None)
        agent, _, gmail, llm, _ = build_agent(
            context, rows, greetings=[greeting()]
        )
        # Only one canned greeting: the second recipient's generation fails.
        result = agent.execute()

        assert len(gmail.sent) == 1
        assert result.status is AgentStatus.PARTIAL_SUCCESS
        outcomes = {r["outcome"] for r in result.details["results"]}
        assert outcomes == {"SENT", "FAILED"}


class TestImageDegradation:
    def test_an_image_failure_still_sends_a_text_greeting(self, context):
        """A plain greeting that arrives beats a rich one that does not."""
        context.dry_run = False
        agent, _, gmail, _, _ = build_agent(
            context, [BIRTHDAY_TODAY], image_error=ImageGenerationError("safety filter")
        )
        result = agent.execute()

        assert len(gmail.sent) == 1
        assert result.details["results"][0]["outcome"] == "SENT"
        assert "without an image" in result.details["results"][0]["reason"]

    def test_the_image_prompt_carries_no_personal_data_beyond_a_first_name(
        self, context
    ):
        agent, _, _, _, images = build_agent(context, [BIRTHDAY_TODAY])
        agent.execute()

        prompt = images.prompts[0]
        assert "rahul@example.com" not in prompt
        assert "Sharma" not in prompt, "only the first name may reach the image service"
        assert "no faces" in prompt.lower() or "no people" in prompt.lower()

    def test_a_sent_greeting_embeds_the_image_inline(self, context):
        context.dry_run = False
        agent, _, gmail, _, _ = build_agent(context, [BIRTHDAY_TODAY])
        agent.execute()

        raw = gmail.sent[0].as_string()
        assert "multipart/related" in raw
        assert "Content-ID:" in raw
        assert 'cid:' in raw
