"""Reads and writes for automation_run and agent_execution."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentExecution, AutomationRun, utcnow


class RunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- runs --------------------------------------------------------------

    def get(self, run_id: str) -> AutomationRun | None:
        return self.session.get(AutomationRun, run_id)

    def get_by_date(self, run_date: date) -> AutomationRun | None:
        stmt = select(AutomationRun).where(AutomationRun.run_date == run_date)
        return self.session.scalars(stmt).first()

    def create(self, run_id: str, run_date: date, *, dry_run: bool) -> AutomationRun:
        run = AutomationRun(
            id=run_id,
            run_date=run_date,
            started_at=utcnow(),
            status="STARTED",
            dry_run=dry_run,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def delete(self, run: AutomationRun) -> None:
        self.session.delete(run)
        self.session.flush()

    def finish(
        self,
        run: AutomationRun,
        *,
        status: str,
        summary: str,
        summary_json: dict | None = None,
    ) -> None:
        run.status = status
        run.summary = summary
        run.summary_json = summary_json
        run.completed_at = utcnow()
        self.session.flush()

    def recent(self, limit: int = 10) -> list[AutomationRun]:
        stmt = (
            select(AutomationRun)
            .order_by(AutomationRun.started_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    # -- executions --------------------------------------------------------

    def start_execution(self, run_id: str, agent_name: str) -> AgentExecution:
        """Create or reset the execution row for one agent within a run."""
        stmt = select(AgentExecution).where(
            AgentExecution.run_id == run_id,
            AgentExecution.agent_name == agent_name,
        )
        execution = self.session.scalars(stmt).first()
        if execution is None:
            execution = AgentExecution(run_id=run_id, agent_name=agent_name)
            self.session.add(execution)
        execution.status = "RUNNING"
        execution.started_at = utcnow()
        execution.completed_at = None
        execution.error = None
        self.session.flush()
        return execution

    def finish_execution(
        self,
        execution: AgentExecution,
        *,
        status: str,
        result_json: dict | None = None,
        error: str | None = None,
        retry_count: int = 0,
    ) -> None:
        execution.status = status
        execution.result_json = result_json
        execution.error = error
        execution.retry_count = retry_count
        execution.completed_at = utcnow()
        self.session.flush()

    def last_successful_execution(self, agent_name: str) -> AgentExecution | None:
        """Most recent COMPLETED or PARTIAL_SUCCESS run of one agent.

        Drives the Gmail incremental window: everything since this timestamp is
        new. A FAILED run is deliberately ignored so its messages are retried.
        """
        stmt = (
            select(AgentExecution)
            .where(
                AgentExecution.agent_name == agent_name,
                AgentExecution.status.in_(("COMPLETED", "PARTIAL_SUCCESS")),
                AgentExecution.completed_at.is_not(None),
            )
            .order_by(AgentExecution.completed_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def last_successful_started_at(self, agent_name: str) -> datetime | None:
        execution = self.last_successful_execution(agent_name)
        return execution.started_at if execution else None
