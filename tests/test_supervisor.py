"""Supervisor orchestration: retry, validation, and workflow independence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from daily_ai_automation.agents.base import DailyAgent
from daily_ai_automation.contracts import AgentResult, AgentStatus
from daily_ai_automation.db import session_scope
from daily_ai_automation.repositories import RunRepository
from daily_ai_automation.supervisor.retry import (
    RetryPolicy,
    TerminalError,
    TransientError,
    is_transient,
    run_with_retry,
)
from daily_ai_automation.supervisor.state_manager import (
    RunIdentity,
    TaskState,
    overall_status,
)
from daily_ai_automation.supervisor.supervisor import AgentRegistration, Supervisor


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


def make_agent(name: str, behaviour):
    """Build an AgentRegistration whose worker runs `behaviour(agent)`.

    The class is built with `type()` rather than by assigning `execute` onto a
    subclass afterwards: ABCMeta computes `__abstractmethods__` at class
    creation, so a late assignment leaves the class still abstract.
    """
    agent_class = type(
        f"_{name.title().replace('_', '')}Agent",
        (DailyAgent,),
        {"name": name, "execute": behaviour},
    )
    return AgentRegistration(name=name, factory=agent_class, is_enabled=lambda s: True)


def succeeds(summary="ok", status=AgentStatus.COMPLETED):
    def execute(self):
        return self.result(status, summary=summary, items_processed=1)

    return execute


def raises(exc_factory, *, times=None):
    state = {"calls": 0}

    def execute(self):
        state["calls"] += 1
        if times is None or state["calls"] <= times:
            raise exc_factory()
        return self.result(AgentStatus.COMPLETED, summary="recovered")

    execute.state = state
    return execute


# --------------------------------------------------------------------------
# Retry classification
# --------------------------------------------------------------------------


class TestIsTransient:
    def test_explicit_markers(self):
        assert is_transient(TransientError("x"))
        assert not is_transient(TerminalError("x"))

    def test_recognised_by_type_name(self):
        class RateLimitError(Exception):
            pass

        assert is_transient(RateLimitError())

    @pytest.mark.parametrize(
        ("status", "expected"), [(429, True), (503, True), (500, True), (404, False), (400, False)]
    )
    def test_http_status_attribute(self, status, expected):
        exc = Exception("boom")
        exc.status_code = status
        assert is_transient(exc) is expected

    def test_googleapiclient_style_response_object(self):
        class Resp:
            status = 429

        exc = Exception("rate limited")
        exc.resp = Resp()
        assert is_transient(exc)

    def test_403_is_not_retried(self):
        """On Google APIs a 403 is a missing scope, not a rate limit."""
        exc = Exception("forbidden")
        exc.status_code = 403
        assert not is_transient(exc)

    def test_an_unknown_exception_is_not_retried(self):
        assert not is_transient(ValueError("nope"))


class TestRetryPolicy:
    def test_backoff_follows_the_configured_schedule(self):
        policy = RetryPolicy(max_attempts=3, backoff_seconds=(30, 120, 300))
        assert policy.delay_for(1) == 0
        assert policy.delay_for(2) == 30
        assert policy.delay_for(3) == 120

    def test_beyond_the_schedule_the_last_delay_repeats(self):
        policy = RetryPolicy(max_attempts=6, backoff_seconds=(30, 120))
        assert policy.delay_for(5) == 120

    def test_a_transient_failure_is_retried_then_succeeds(self):
        calls = {"n": 0}
        slept = []

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("try again")
            return "done"

        value, outcome = run_with_retry(
            flaky,
            RetryPolicy(max_attempts=3, backoff_seconds=(30, 120)),
            sleep=slept.append,
        )
        assert value == "done"
        assert outcome.attempts == 3
        assert slept == [30, 120]

    def test_a_terminal_failure_is_not_retried(self):
        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise TerminalError("bad credentials")

        with pytest.raises(TerminalError):
            run_with_retry(bad, RetryPolicy(max_attempts=3), sleep=lambda _: None)
        assert calls["n"] == 1, "a terminal error must not burn retries"

    def test_retries_are_bounded(self):
        calls = {"n": 0}

        def always_bad():
            calls["n"] += 1
            raise TransientError("still down")

        with pytest.raises(TransientError):
            run_with_retry(
                always_bad, RetryPolicy(max_attempts=3), sleep=lambda _: None
            )
        assert calls["n"] == 3


# --------------------------------------------------------------------------
# Run identity and status collapsing
# --------------------------------------------------------------------------


class TestRunIdentity:
    def test_the_run_date_comes_from_the_configured_timezone(self):
        from zoneinfo import ZoneInfo

        kolkata = ZoneInfo("Asia/Kolkata")
        # 22:00 UTC on 6 September is already 03:30 on 7 September in Kolkata.
        utc_moment = datetime(2026, 9, 6, 22, 0, tzinfo=timezone.utc)
        identity = RunIdentity.create(kolkata, now=utc_moment)
        assert identity.run_date.isoformat() == "2026-09-07"
        assert identity.run_id.startswith("2026-09-07-")


class TestOverallStatus:
    def test_all_completed(self):
        assert overall_status({"a": TaskState.COMPLETED}) == "COMPLETED"

    def test_all_failed(self):
        assert overall_status({"a": TaskState.FAILED}) == "FAILED"

    def test_one_failure_alongside_a_success_is_partial(self):
        assert (
            overall_status({"a": TaskState.COMPLETED, "b": TaskState.FAILED})
            == "PARTIAL_SUCCESS"
        )

    def test_a_partial_worker_makes_the_run_partial(self):
        assert (
            overall_status({"a": TaskState.COMPLETED, "b": TaskState.PARTIAL_SUCCESS})
            == "PARTIAL_SUCCESS"
        )

    def test_a_skipped_workflow_does_not_downgrade_the_run(self):
        """Disabling Gmail must not make every run look partial."""
        assert (
            overall_status({"a": TaskState.COMPLETED, "b": TaskState.SKIPPED})
            == "COMPLETED"
        )

    def test_everything_skipped_is_completed(self):
        assert overall_status({"a": TaskState.SKIPPED}) == "COMPLETED"


# --------------------------------------------------------------------------
# End-to-end orchestration
# --------------------------------------------------------------------------


class TestSupervisorRun:
    def test_an_empty_registry_completes(self, settings, session_factory):
        report = Supervisor(settings, session_factory, []).run()
        assert report.status == "COMPLETED"
        assert report.results == {}

    def test_a_disabled_worker_is_skipped_not_run(self, settings, session_factory):
        called = {"n": 0}

        def execute(self):
            called["n"] += 1
            return self.result(AgentStatus.COMPLETED)

        registration = make_agent("off_agent", execute)
        registration = AgentRegistration(
            name="off_agent", factory=registration.factory, is_enabled=lambda s: False
        )
        report = Supervisor(settings, session_factory, [registration]).run()

        assert called["n"] == 0
        assert report.states["off_agent"] is TaskState.SKIPPED
        assert report.status == "COMPLETED"

    def test_one_workflow_failing_does_not_block_the_other(
        self, settings, session_factory
    ):
        """PRD section 32: independent workflows must not block each other."""
        registrations = [
            make_agent("gmail", raises(lambda: TerminalError("gmail is down"))),
            make_agent("occasion", succeeds("3 greetings sent")),
        ]
        report = Supervisor(
            settings, session_factory, registrations, sleep=lambda _: None
        ).run()

        assert report.results["gmail"].status is AgentStatus.FAILED
        assert report.results["occasion"].status is AgentStatus.COMPLETED
        assert report.results["occasion"].summary == "3 greetings sent"
        assert report.status == "PARTIAL_SUCCESS"

    def test_a_transient_failure_is_retried_and_recorded(
        self, settings, session_factory
    ):
        behaviour = raises(lambda: TransientError("flaky"), times=1)
        registrations = [make_agent("flaky_agent", behaviour)]
        report = Supervisor(
            settings, session_factory, registrations, sleep=lambda _: None
        ).run()

        assert report.results["flaky_agent"].status is AgentStatus.COMPLETED
        with session_scope(session_factory) as session:
            execution = RunRepository(session).last_successful_execution("flaky_agent")
            assert execution.retry_count == 1

    def test_a_worker_returning_a_non_terminal_status_is_marked_failed(
        self, settings, session_factory
    ):
        """PRD 7.3: never claim success the worker has not confirmed."""
        registrations = [
            make_agent("liar", succeeds(status=AgentStatus.IN_PROGRESS))
        ]
        report = Supervisor(settings, session_factory, registrations).run()

        assert report.results["liar"].status is AgentStatus.FAILED
        assert any("did not confirm" in e for e in report.results["liar"].errors)

    def test_a_worker_returning_the_wrong_run_id_is_rejected(
        self, settings, session_factory
    ):
        def execute(self):
            return AgentResult(
                run_id="some-other-run",
                agent="impostor",
                status=AgentStatus.COMPLETED,
                started_at=self.started_at,
            )

        registrations = [make_agent("impostor", execute)]
        report = Supervisor(settings, session_factory, registrations).run()
        assert report.results["impostor"].status is AgentStatus.FAILED

    def test_the_run_row_records_the_final_status(self, settings, session_factory):
        registrations = [make_agent("worker", succeeds())]
        report = Supervisor(settings, session_factory, registrations).run()

        with session_scope(session_factory) as session:
            row = RunRepository(session).get(report.run_id)
            assert row.status == "COMPLETED"
            assert row.dry_run is True
            assert row.completed_at is not None
            assert row.summary_json["results"]["worker"]["status"] == "COMPLETED"

    def test_a_worker_crash_does_not_propagate_out_of_the_run(
        self, settings, session_factory
    ):
        registrations = [make_agent("boom", raises(lambda: RuntimeError("kaboom")))]
        report = Supervisor(
            settings, session_factory, registrations, sleep=lambda _: None
        ).run()
        assert report.status == "FAILED"
        assert "kaboom" in report.results["boom"].errors[0]


class TestCancelledStatus:
    def test_a_cancelled_worker_does_not_crash_the_supervisor(
        self, settings, session_factory
    ):
        """AgentStatus.CANCELLED must map onto a TaskState, not raise."""
        registrations = [
            make_agent("cancelled_agent", succeeds(status=AgentStatus.CANCELLED))
        ]
        report = Supervisor(settings, session_factory, registrations).run()

        assert report.states["cancelled_agent"] is TaskState.CANCELLED
        assert report.status == "FAILED", "cancelled work must not read as success"

    def test_cancelled_alongside_a_success_is_partial(self):
        assert (
            overall_status(
                {"a": TaskState.COMPLETED, "b": TaskState.CANCELLED}
            )
            == "PARTIAL_SUCCESS"
        )
