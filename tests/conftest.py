"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from daily_ai_automation.agents.base import RunContext
from daily_ai_automation.config.settings import Settings
from daily_ai_automation.contracts import (
    EmailCategory,
    EmailClassification,
    Priority,
    RecommendedAction,
)
from daily_ai_automation.db import create_db_engine, create_session_factory, init_schema
from daily_ai_automation.integrations.gmail import GmailMessage
from daily_ai_automation.repositories import AuditRepository


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A fully-populated Settings that points at a temporary directory."""
    return Settings.model_validate(
        {
            "dry_run": True,
            "database": {"url": f"sqlite:///{(tmp_path / 'test.db').as_posix()}"},
            "occasion": {
                "drive_file_id": "test-file-id",
                "sender_name": "Test Sender",
            },
            "project_root": tmp_path,
        }
    )


@pytest.fixture
def session_factory(settings):
    engine = create_db_engine(settings.database_url)
    init_schema(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory) -> Session:
    with session_factory() as active:
        yield active


@pytest.fixture
def context(settings, session) -> RunContext:
    return RunContext(
        run_id="2026-09-07-090000",
        run_date=datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc),
        settings=settings,
        session=session,
        audit=AuditRepository(session),
        dry_run=settings.dry_run,
    )


def make_message(
    *,
    message_id: str = "msg-1",
    sender: str = "News <news@example.com>",
    subject: str = "This week in widgets",
    body: str = "Our latest offers on widgets.",
    snippet: str = "",
    labels: list[str] | None = None,
    minutes_ago: int = 30,
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender=sender,
        to="me@example.com",
        subject=subject,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        snippet=snippet or body[:80],
        body=body,
        label_ids=labels or ["INBOX"],
    )


def make_classification(
    *,
    message_id: str = "msg-1",
    category: EmailCategory = EmailCategory.NEWSLETTER,
    requires_reply: bool = False,
    priority: Priority = Priority.LOW,
    confidence: float = 0.98,
    action: RecommendedAction = RecommendedAction.TO_DELETE,
    injection: bool = False,
    suggested_reply: str = "",
    why_it_matters: str = "",
    suggested_deadline: str = "",
) -> EmailClassification:
    return EmailClassification(
        message_id=message_id,
        category=category,
        requires_reply=requires_reply,
        priority=priority,
        confidence=confidence,
        summary="A summary.",
        recommended_action=action,
        reason="Because.",
        contains_injection_attempt=injection,
        suggested_reply=suggested_reply,
        why_it_matters=why_it_matters,
        suggested_deadline=suggested_deadline,
    )
