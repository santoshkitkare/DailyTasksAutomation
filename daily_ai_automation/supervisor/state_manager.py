"""Daily run identity and task state transitions (PRD section 30)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class DuplicateRunError(RuntimeError):
    """A run already exists for this date (PRD section 42)."""


@dataclass(frozen=True)
class RunIdentity:
    """The run_id and the local calendar date it belongs to.

    The date comes from the *configured* timezone, not the machine's. A laptop
    that travels must not produce two runs for one Indian calendar day.
    """

    run_id: str
    run_date: date
    local_now: datetime

    @classmethod
    def create(cls, tz: ZoneInfo, *, now: datetime | None = None) -> "RunIdentity":
        local_now = (now or datetime.now(tz)).astimezone(tz)
        return cls(
            run_id=local_now.strftime("%Y-%m-%d-%H%M%S"),
            run_date=local_now.date(),
            local_now=local_now,
        )


def overall_status(task_states: dict[str, TaskState]) -> str:
    """Collapse per-task outcomes into the run status (PRD section 32).

    One workflow failing must not mask another succeeding, so a mixed result is
    PARTIAL_SUCCESS rather than either extreme. Skipped tasks (disabled in
    config) are ignored: disabling Gmail should not make every run partial.
    """
    considered = {
        name: state
        for name, state in task_states.items()
        if state is not TaskState.SKIPPED
    }
    if not considered:
        return "COMPLETED"

    states = set(considered.values())
    # CANCELLED counts as a failure: the work did not happen, and
    # reporting it as success would be exactly the lie PRD 7.3 forbids.
    failure_states = {TaskState.FAILED, TaskState.TIMEOUT, TaskState.CANCELLED}

    if states <= {TaskState.COMPLETED}:
        return "COMPLETED"
    if states & failure_states and not (
        states & {TaskState.COMPLETED, TaskState.PARTIAL_SUCCESS}
    ):
        return "FAILED"
    return "PARTIAL_SUCCESS"
