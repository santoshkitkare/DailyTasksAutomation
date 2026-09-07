"""The bounded orchestrator (PRD sections 7, 26, 49).

Plan -> delegate -> monitor -> validate -> stop -> report. It is a loop over a
known list of workers with a retry policy, not a reasoning agent: nothing here
calls an LLM. Every decision it makes is deterministic and inspectable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from ..agents.base import DailyAgent, RunContext
from ..config.settings import Settings
from ..contracts import AgentResult, AgentStatus
from ..db import session_scope
from ..models import utcnow
from ..repositories import AuditRepository, RunRepository
from .retry import RetryPolicy, TerminalError, run_with_retry
from .state_manager import DuplicateRunError, RunIdentity, TaskState, overall_status

logger = logging.getLogger(__name__)

#: How to build a worker once the supervisor has assembled its context.
AgentFactory = Callable[[RunContext], DailyAgent]


@dataclass(frozen=True)
class AgentRegistration:
    """A worker the supervisor knows how to run, and when it should."""

    name: str
    factory: AgentFactory
    is_enabled: Callable[[Settings], bool]


@dataclass
class RunReport:
    """What the supervisor produces. Rendered by the reporting layer."""

    run_id: str
    run_date: str
    dry_run: bool
    status: str = "STARTED"
    results: dict[str, AgentResult] = field(default_factory=dict)
    states: dict[str, TaskState] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_date": self.run_date,
            "dry_run": self.dry_run,
            "status": self.status,
            "states": {name: str(state) for name, state in self.states.items()},
            "results": {
                name: result.model_dump(mode="json")
                for name, result in self.results.items()
            },
            "errors": self.errors,
        }


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        registrations: list[AgentRegistration],
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.registrations = registrations
        self._sleep = sleep
        self.policy = RetryPolicy(
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=tuple(settings.retry.backoff_seconds),
        )

    # -- entry point -------------------------------------------------------

    def run(self, *, force: bool = False, now: datetime | None = None) -> RunReport:
        identity = RunIdentity.create(self.settings.scheduler.tzinfo, now=now)
        report = RunReport(
            run_id=identity.run_id,
            run_date=identity.run_date.isoformat(),
            dry_run=self.settings.dry_run,
        )

        with session_scope(self.session_factory) as session:
            self._claim_run(session, identity, force=force)

        mode = " (DRY RUN - no external writes)" if self.settings.dry_run else ""
        logger.info(
            "Daily run started%s",
            mode,
            extra={"run_id": identity.run_id, "status": "STARTED"},
        )

        for registration in self.registrations:
            self._run_one(registration, identity, report)

        report.status = overall_status(report.states)

        with session_scope(self.session_factory) as session:
            repo = RunRepository(session)
            run = repo.get(identity.run_id)
            if run is not None:
                repo.finish(
                    run,
                    status=report.status,
                    summary=_one_line_summary(report),
                    summary_json=report.to_json(),
                )

        logger.info(
            "Daily run finished: %s",
            report.status,
            extra={"run_id": identity.run_id, "status": report.status},
        )
        return report

    # -- internals ---------------------------------------------------------

    def _claim_run(
        self, session: Session, identity: RunIdentity, *, force: bool
    ) -> None:
        """Create the run row, refusing a second run for the same date."""
        repo = RunRepository(session)
        existing = repo.get_by_date(identity.run_date)
        if existing is not None:
            if not force:
                raise DuplicateRunError(
                    f"A run already exists for {identity.run_date} "
                    f"(id={existing.id}, status={existing.status}). "
                    "Re-run with --force to replace it."
                )
            logger.warning(
                "Replacing existing run %s for %s (--force)",
                existing.id,
                identity.run_date,
                extra={"run_id": identity.run_id},
            )
            repo.delete(existing)
        repo.create(identity.run_id, identity.run_date, dry_run=self.settings.dry_run)

    def _run_one(
        self,
        registration: AgentRegistration,
        identity: RunIdentity,
        report: RunReport,
    ) -> None:
        name = registration.name

        if not registration.is_enabled(self.settings):
            report.states[name] = TaskState.SKIPPED
            logger.info(
                "%s is disabled in config; skipping",
                name,
                extra={"run_id": identity.run_id, "agent": name, "status": "SKIPPED"},
            )
            return

        report.states[name] = TaskState.RUNNING
        started = utcnow()
        attempts = 0

        def attempt() -> AgentResult:
            # A fresh session per attempt: a failed attempt must not leave a
            # poisoned transaction for the retry to inherit.
            with session_scope(self.session_factory) as session:
                context = RunContext(
                    run_id=identity.run_id,
                    run_date=identity.local_now,
                    settings=self.settings,
                    session=session,
                    audit=AuditRepository(session),
                    dry_run=self.settings.dry_run,
                )
                agent = registration.factory(context)
                return agent.execute()

        def on_retry(attempt_no: int, exc: BaseException, delay: int) -> None:
            report.states[name] = TaskState.RETRYING
            logger.warning(
                "%s attempt %d failed (%s); retrying in %ds",
                name,
                attempt_no,
                type(exc).__name__,
                delay,
                extra={
                    "run_id": identity.run_id,
                    "agent": name,
                    "status": "RETRYING",
                    "error_code": type(exc).__name__,
                },
            )

        try:
            kwargs: dict = {"on_retry": on_retry}
            if self._sleep is not None:
                kwargs["sleep"] = self._sleep
            result, outcome = run_with_retry(attempt, self.policy, **kwargs)
            attempts = outcome.attempts
            result = self._validate(name, identity.run_id, result)
        except BaseException as exc:  # noqa: BLE001 - recorded, never re-raised
            attempts = self.policy.max_attempts
            result = _failure_result(
                identity.run_id,
                name,
                started,
                exc,
                terminal=isinstance(exc, TerminalError),
            )
            report.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.error(
                "%s failed after %d attempt(s): %s",
                name,
                attempts,
                exc,
                exc_info=True,
                extra={
                    "run_id": identity.run_id,
                    "agent": name,
                    "status": "FAILED",
                    "error_code": type(exc).__name__,
                },
            )

        report.results[name] = result
        report.states[name] = TaskState(str(result.status))
        report.errors.extend(f"{name}: {message}" for message in result.errors)

        duration = (utcnow() - started).total_seconds()
        with session_scope(self.session_factory) as session:
            repo = RunRepository(session)
            execution = repo.start_execution(identity.run_id, name)
            execution.started_at = started
            repo.finish_execution(
                execution,
                status=str(result.status),
                result_json=result.model_dump(mode="json"),
                error="; ".join(result.errors) or None,
                retry_count=max(attempts - 1, 0),
            )

        # Stopping the sub-agent: the worker object goes out of scope here and
        # nothing retains a reference to it (PRD section 7.3).
        logger.info(
            "%s finished: %s (%s)",
            name,
            result.status,
            result.summary or "no summary",
            extra={
                "run_id": identity.run_id,
                "agent": name,
                "status": str(result.status),
                "duration": round(duration, 2),
            },
        )

    def _validate(self, name: str, run_id: str, result: AgentResult) -> AgentResult:
        """Never claim success the worker did not confirm (PRD section 7.3)."""
        problems = []
        if result.agent != name:
            problems.append(f"agent field is {result.agent!r}, expected {name!r}")
        if result.run_id != run_id:
            problems.append(f"run_id is {result.run_id!r}, expected {run_id!r}")
        if not result.status.is_terminal:
            problems.append(
                f"returned non-terminal status {result.status}; the worker did "
                "not confirm completion"
            )
        if not problems:
            return result

        reason = "; ".join(problems)
        logger.error(
            "%s returned an invalid result: %s",
            name,
            reason,
            extra={"run_id": run_id, "agent": name, "status": "FAILED"},
        )
        return result.model_copy(
            update={
                "status": AgentStatus.FAILED,
                "errors": [*result.errors, f"Supervisor validation failed: {reason}"],
            }
        )


def _failure_result(
    run_id: str,
    name: str,
    started: datetime,
    exc: BaseException,
    *,
    terminal: bool,
) -> AgentResult:
    status = (
        AgentStatus.TIMEOUT if isinstance(exc, TimeoutError) else AgentStatus.FAILED
    )
    prefix = "Terminal failure" if terminal else "Failed"
    return AgentResult(
        run_id=run_id,
        agent=name,
        status=status,
        started_at=started,
        completed_at=utcnow(),
        errors=[f"{type(exc).__name__}: {exc}"],
        summary=f"{prefix}: {exc}",
    )


def _one_line_summary(report: RunReport) -> str:
    parts = [
        f"{name}={result.status}" for name, result in sorted(report.results.items())
    ]
    skipped = [
        name for name, state in report.states.items() if state is TaskState.SKIPPED
    ]
    parts.extend(f"{name}=SKIPPED" for name in sorted(skipped))
    return f"{report.status} ({', '.join(parts)})" if parts else report.status
