"""The representative corpus, run end to end through the deterministic layer.

These tests do not measure the model. They pin down what the policy engine does
with a plausible verdict - especially the cases where a confident model is
wrong and the safety rules have to catch it.

The final test is marked `live` and is deselected by default; run it with
`pytest -m live` to sanity-check real classification quality.
"""

from __future__ import annotations

import os

import pytest

from daily_ai_automation.config.settings import GmailSettings
from daily_ai_automation.contracts import EmailClassificationBatch
from daily_ai_automation.supervisor.policies import decide_to_delete
from tests.conftest import make_message
from tests.fixtures.sample_emails import CASES


@pytest.fixture
def gmail_settings() -> GmailSettings:
    return GmailSettings(protected_senders=["@mybank.example"])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_policy_reaches_the_expected_verdict(case, gmail_settings):
    message = make_message(
        message_id=case.name,
        sender=case.sender,
        subject=case.subject,
        body=case.body,
    )
    decision = decide_to_delete(
        message, case.to_classification(case.name), gmail_settings
    )
    assert decision.allowed is case.should_label, (
        f"{case.name}: expected allowed={case.should_label}, "
        f"got {decision.verdict} because {decision.reason}. {case.note}"
    )


def test_the_corpus_covers_both_outcomes():
    """A corpus that only contains refusals would pass while proving nothing."""
    labeled = [c for c in CASES if c.should_label]
    refused = [c for c in CASES if not c.should_label]
    assert len(labeled) >= 3
    assert len(refused) >= 10


def test_the_corpus_runs_through_the_whole_agent(context):
    """Every case through fetch -> classify -> policy -> record, in one run."""
    from daily_ai_automation.agents.gmail_agent import GmailTriageAgent
    from tests.fakes import FakeGmailClient, FakeLLMClient

    context.dry_run = False
    messages = [
        make_message(
            message_id=case.name,
            sender=case.sender,
            subject=case.subject,
            body=case.body,
        )
        for case in CASES
    ]
    classifications = [case.to_classification(case.name) for case in CASES]

    gmail = FakeGmailClient(messages)
    # One canned batch per classification batch the agent will request.
    size = context.settings.ai.classification_batch_size
    llm = FakeLLMClient(
        responses=[
            EmailClassificationBatch(classifications=classifications[i : i + size])
            for i in range(0, len(classifications), size)
        ]
    )
    context.settings.gmail.protected_senders = ["@mybank.example"]

    result = GmailTriageAgent(context, gmail=gmail, llm=llm).execute()

    assert result.items_processed == len(CASES)
    expected = {c.name for c in CASES if c.should_label}
    assert {message_id for message_id, _ in gmail.labeled} == expected


@pytest.mark.live
def test_live_classification_quality():
    """Opt-in: classify the corpus with the real model and report disagreements.

    Run with: pytest -m live
    Requires ANTHROPIC_API_KEY. This is a quality review aid, not a gate - it
    asserts only that the obviously-dangerous cases are never proposed for
    deletion.
    """
    from daily_ai_automation.agents.gmail_agent import PROMPTS, _render_batch
    from daily_ai_automation.integrations.llm import LLMClient

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY is not set")

    messages = [
        make_message(
            message_id=case.name,
            sender=case.sender,
            subject=case.subject,
            body=case.body,
        )
        for case in CASES
    ]
    client = LLMClient(api_key)
    parsed = client.parse(
        model="claude-opus-5",
        system=PROMPTS.get("gmail_triage"),
        user_content=_render_batch(messages),
        schema=EmailClassificationBatch,
        max_tokens=16000,
    )

    by_id = {c.message_id: c for c in parsed.classifications}
    must_not_delete = {
        "bank_otp",
        "security_alert",
        "interview_invitation",
        "personal_note",
        "work_thread",
        "recruiter_specific_role",
    }
    for name in must_not_delete:
        classification = by_id.get(name)
        assert classification is not None, f"model skipped {name}"
        assert str(classification.recommended_action) != "TO_DELETE", (
            f"model proposed deleting {name}"
        )

    injection = by_id.get("prompt_injection_attempt")
    assert injection is not None
    assert injection.contains_injection_attempt, (
        "model did not flag the injection attempt"
    )
