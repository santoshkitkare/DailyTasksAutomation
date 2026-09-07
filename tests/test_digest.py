"""Daily report rendering (PRD section 33)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_ai_automation.contracts import (
    AgentResult,
    AgentStatus,
    EmailCategory,
    Priority,
    RecommendedAction,
    TriagedEmail,
)
from daily_ai_automation.reporting.digest import render
from daily_ai_automation.supervisor.state_manager import TaskState
from daily_ai_automation.supervisor.supervisor import RunReport
from tests.conftest import make_classification

NOW = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)


def triaged(**kwargs) -> dict:
    """Build a serialised TriagedEmail; envelope fields are kept separate from
    classification fields so neither factory sees the other's arguments."""
    message_id = kwargs.pop("message_id", "m1")
    sender = kwargs.pop("sender", "Anita <anita@agency.example>")
    subject = kwargs.pop("subject", "Lead Python Engineer opportunity")
    return TriagedEmail(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender=sender,
        subject=subject,
        received_at=NOW,
        classification=make_classification(message_id=message_id, **kwargs),
    ).model_dump(mode="json")


def gmail_result(notify=None, counts=None, status=AgentStatus.COMPLETED) -> AgentResult:
    return AgentResult(
        run_id="r1",
        agent="gmail_triage_agent",
        status=status,
        started_at=NOW,
        completed_at=NOW,
        items_processed=47,
        summary="Processed 47 new emails.",
        details={
            "notify": notify or [],
            "counts": counts
            or {
                "processed": 47,
                "job_opportunities": 3,
                "requires_reply": 2,
                "high_priority": 2,
                "labeled": 11,
                "would_label": 0,
                "injection_attempts": 0,
            },
        },
    )


def occasion_result(results=None, invalid=None) -> AgentResult:
    return AgentResult(
        run_id="r1",
        agent="occasion_agent",
        status=AgentStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW,
        summary="3 sent.",
        details={"results": results or [], "invalid_rows": invalid or []},
    )


def build_report(**kwargs) -> RunReport:
    return RunReport(
        run_id=kwargs.get("run_id", "2026-09-07-090000"),
        run_date=kwargs.get("run_date", "2026-09-07"),
        dry_run=kwargs.get("dry_run", False),
        status=kwargs.get("status", "COMPLETED"),
        results=kwargs.get("results", {}),
        states=kwargs.get("states", {}),
        errors=kwargs.get("errors", []),
    )


class TestStructure:
    def test_subject_carries_the_date_and_status(self):
        rendered = render(build_report(status="PARTIAL_SUCCESS"))
        assert "2026-09-07" in rendered.subject
        assert "Partial Success" in rendered.subject

    def test_dry_run_is_announced_in_both_renderings(self):
        rendered = render(build_report(dry_run=True))
        assert "[DRY RUN]" in rendered.subject
        assert "DRY RUN" in rendered.text
        assert "Dry run" in rendered.html

    def test_every_prd_section_is_present(self):
        rendered = render(
            build_report(
                results={
                    "gmail_triage_agent": gmail_result(),
                    "occasion_agent": occasion_result(),
                }
            )
        )
        for heading in ("Gmail", "Occasions", "Action Required", "Errors"):
            assert heading in rendered.text

    def test_the_html_is_self_contained(self):
        rendered = render(build_report(results={"gmail_triage_agent": gmail_result()}))
        assert rendered.html.startswith("<html>")
        assert "http://" not in rendered.html.replace("https://mail.google.com", "")


class TestGmailSection:
    def test_counts_are_reported(self):
        rendered = render(build_report(results={"gmail_triage_agent": gmail_result()}))
        assert "New emails processed: 47" in rendered.text
        assert "Emails labeled ToDelete: 11" in rendered.text

    def test_dry_run_reports_would_be_labeled(self):
        counts = {"processed": 5, "labeled": 0, "would_label": 4}
        rendered = render(
            build_report(dry_run=True, results={"gmail_triage_agent": gmail_result(counts=counts)})
        )
        assert "Emails would be labeled ToDelete: 4" in rendered.text

    def test_a_skipped_workflow_says_so(self):
        rendered = render(
            build_report(states={"gmail_triage_agent": TaskState.SKIPPED})
        )
        assert "Disabled in configuration" in rendered.text

    def test_injection_attempts_are_surfaced(self):
        counts = {"processed": 3, "labeled": 0, "would_label": 0, "injection_attempts": 2}
        rendered = render(build_report(results={"gmail_triage_agent": gmail_result(counts=counts)}))
        assert "Prompt-injection attempts detected: 2" in rendered.text


class TestActionRequired:
    def test_reply_required_emails_appear_with_their_link_and_reply(self):
        item = triaged(
            message_id="job1",
            category=EmailCategory.JOB_OPPORTUNITY,
            priority=Priority.HIGH,
            requires_reply=True,
            action=RecommendedAction.REPLY,
            suggested_reply="Thanks for reaching out - I would be glad to hear more.",
        )
        rendered = render(
            build_report(results={"gmail_triage_agent": gmail_result(notify=[item])})
        )
        assert "Lead Python Engineer opportunity" in rendered.text
        assert "thread-job1" in rendered.text
        assert "Thanks for reaching out" in rendered.text
        assert "Suggested reply" in rendered.html

    def test_items_are_ordered_by_priority(self):
        low = triaged(
            message_id="low",
            category=EmailCategory.RECRUITER,
            priority=Priority.LOW,
            requires_reply=True,
        )
        critical = triaged(
            message_id="crit",
            category=EmailCategory.JOB_OPPORTUNITY,
            priority=Priority.CRITICAL,
            requires_reply=True,
        )
        rendered = render(
            build_report(
                results={"gmail_triage_agent": gmail_result(notify=[low, critical])}
            )
        )
        assert rendered.text.index("thread-crit") < rendered.text.index("thread-low")

    def test_nothing_actionable_says_none(self):
        rendered = render(build_report(results={"gmail_triage_agent": gmail_result()}))
        assert "Action Required\n---------------\nNone" in rendered.text

    def test_a_notified_but_non_actionable_email_is_not_listed(self):
        """MEDIUM-priority mail is worth notifying about but needs no action."""
        item = triaged(message_id="fyi", priority=Priority.MEDIUM)
        rendered = render(
            build_report(results={"gmail_triage_agent": gmail_result(notify=[item])})
        )
        assert "thread-fyi" not in rendered.text


class TestOccasionSection:
    def _result(self, outcome, event_type="Birthday"):
        return {
            "contact": {
                "row_number": 2,
                "event_type": event_type,
                "event_date": "1990-09-07",
                "full_name": "Rahul Sharma",
                "email_address": "rahul@example.com",
                "mobile_number": "",
                "relationship": None,
            },
            "outcome": outcome,
            "reason": "",
            "provider_message_id": "",
            "years": None,
        }

    def test_counts_by_outcome(self):
        results = [
            self._result("SENT"),
            self._result("SENT", "Anniversary"),
            self._result("SKIPPED_DUPLICATE"),
        ]
        rendered = render(
            build_report(results={"occasion_agent": occasion_result(results)})
        )
        assert "Birthdays today: 2" in rendered.text
        assert "Anniversaries today: 1" in rendered.text
        assert "Greetings sent: 2" in rendered.text
        assert "Skipped duplicates: 1" in rendered.text

    def test_invalid_rows_are_listed(self):
        invalid = [
            {"row_number": 5, "reason": "EmailAddress 'nope' is not a valid address", "raw": {}}
        ]
        rendered = render(
            build_report(results={"occasion_agent": occasion_result(invalid=invalid)})
        )
        assert "Invalid contact rows: 1" in rendered.text
        assert "Row 5" in rendered.text
        assert "Row 5" in rendered.html


class TestErrors:
    def test_errors_and_warnings_are_both_shown(self):
        result = gmail_result(status=AgentStatus.PARTIAL_SUCCESS)
        result.warnings.append("Hit the max_emails_per_run cap")
        rendered = render(
            build_report(
                results={"gmail_triage_agent": result},
                errors=["occasion_agent: Drive file not found"],
            )
        )
        assert "Drive file not found" in rendered.text
        assert "max_emails_per_run" in rendered.text

    def test_html_escapes_untrusted_subjects(self):
        """A subject is attacker-controlled text; it must not become markup."""
        item = triaged(
            message_id="x",
            requires_reply=True,
            subject="<script>alert(1)</script>",
        )
        rendered = render(
            build_report(results={"gmail_triage_agent": gmail_result(notify=[item])})
        )
        assert "<script>" not in rendered.html
        assert "&lt;script&gt;" in rendered.html
