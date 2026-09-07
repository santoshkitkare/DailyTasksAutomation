"""Policy-engine tests.

This is the component that can damage a real mailbox, so it gets the densest
coverage in the suite: a table over every veto, plus the one combination that
is allowed to pass.
"""

from __future__ import annotations

import pytest

from daily_ai_automation.config.settings import GmailSettings
from daily_ai_automation.contracts import EmailCategory, Priority, RecommendedAction
from daily_ai_automation.supervisor.policies import (
    decide_to_delete,
    find_sensitive_signal,
    matched_protected_sender,
    should_notify,
)
from tests.conftest import make_classification, make_message


@pytest.fixture
def gmail_settings() -> GmailSettings:
    return GmailSettings(
        auto_label_threshold=0.95,
        auto_label_allowed_categories=["NEWSLETTER", "PROMOTION", "SOCIAL"],
        protected_senders=["@mybank.example", "mum@family.example"],
    )


def test_allows_the_clear_case(gmail_settings):
    decision = decide_to_delete(
        make_message(), make_classification(), gmail_settings
    )
    assert decision.allowed
    assert "NEWSLETTER" in decision.reason


@pytest.mark.parametrize(
    ("kwargs", "expected_fragment"),
    [
        ({"action": RecommendedAction.READ}, "not TO_DELETE"),
        ({"injection": True}, "prompt-injection"),
        ({"requires_reply": True}, "requiring a reply"),
        ({"priority": Priority.HIGH}, "above the LOW threshold"),
        ({"priority": Priority.MEDIUM}, "above the LOW threshold"),
        ({"category": EmailCategory.FINANCIAL}, "protected"),
        ({"category": EmailCategory.JOB_OPPORTUNITY}, "protected"),
        ({"category": EmailCategory.PERSONAL}, "protected"),
        ({"category": EmailCategory.NOTIFICATION}, "auto_label_allowed_categories"),
        ({"category": EmailCategory.OTHER}, "auto_label_allowed_categories"),
        ({"confidence": 0.94}, "below the"),
        ({"confidence": 0.5}, "below the"),
    ],
)
def test_each_classification_veto(gmail_settings, kwargs, expected_fragment):
    decision = decide_to_delete(
        make_message(), make_classification(**kwargs), gmail_settings
    )
    assert not decision.allowed
    assert expected_fragment in decision.reason


def test_threshold_is_inclusive(gmail_settings):
    """0.95 exactly must pass, or the documented threshold is a lie."""
    decision = decide_to_delete(
        make_message(), make_classification(confidence=0.95), gmail_settings
    )
    assert decision.allowed


@pytest.mark.parametrize(
    "sender",
    ["alerts@mybank.example", "Bank <noreply@mybank.example>", "mum@family.example"],
)
def test_protected_senders_veto(gmail_settings, sender):
    decision = decide_to_delete(
        make_message(sender=sender), make_classification(), gmail_settings
    )
    assert not decision.allowed
    assert "protected pattern" in decision.reason


@pytest.mark.parametrize(
    "subject",
    [
        "Your one-time passcode is 123456",
        "Security alert: new sign-in on your account",
        "Your invoice for September",
        "Payment failed for your subscription",
        "Suspicious login detected",
        "Your account is locked",
        "Interview confirmation",
        "Your passport application update",
        "Password reset requested",
    ],
)
def test_sensitive_subjects_veto(gmail_settings, subject):
    """A high-confidence PROMOTION verdict must still lose to a security phrase."""
    decision = decide_to_delete(
        make_message(subject=subject),
        make_classification(category=EmailCategory.PROMOTION, confidence=0.99),
        gmail_settings,
    )
    assert not decision.allowed
    assert "subject or preview mentions" in decision.reason


def test_sensitive_phrase_in_snippet_also_vetoes(gmail_settings):
    message = make_message(
        subject="Monthly update", snippet="Your OTP for the transfer is 998877"
    )
    decision = decide_to_delete(message, make_classification(), gmail_settings)
    assert not decision.allowed


@pytest.mark.parametrize("label", ["STARRED", "IMPORTANT"])
def test_user_engagement_vetoes(gmail_settings, label):
    message = make_message(labels=["INBOX", label])
    decision = decide_to_delete(message, make_classification(), gmail_settings)
    assert not decision.allowed
    assert "starred or marked important" in decision.reason


def test_empty_protected_sender_pattern_does_not_match_everything():
    """A stray blank line in config must not veto every message."""
    message = make_message(sender="news@example.com")
    assert matched_protected_sender(message, ["", "   ", "@bank.example"]) == ""


def test_find_sensitive_signal_returns_the_phrase():
    message = make_message(subject="Your receipt from Acme")
    assert find_sensitive_signal(message).lower() == "receipt"
    assert find_sensitive_signal(make_message(subject="Widgets weekly")) == ""


class TestShouldNotify:
    def test_reply_required_is_reported(self):
        assert should_notify(make_classification(requires_reply=True))

    def test_job_opportunity_is_reported_even_at_low_priority(self):
        assert should_notify(
            make_classification(
                category=EmailCategory.JOB_OPPORTUNITY, priority=Priority.LOW
            )
        )

    def test_injection_attempt_is_always_reported(self):
        assert should_notify(make_classification(injection=True))

    def test_plain_newsletter_is_not_reported(self):
        assert not should_notify(make_classification())
