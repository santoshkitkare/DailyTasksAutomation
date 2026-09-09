"""Rebuild a RunReport from a stored automation_run row.

The supervisor's in-memory RunReport is never persisted directly - only its
JSON projection (AutomationRun.summary_json). Both the CLI `report` command and
the dashboard's run-detail page need to turn that JSON back into the same typed
object the digest renderer expects, so the conversion lives here once.
"""

from __future__ import annotations

from ..contracts import AgentResult
from ..models import AutomationRun
from ..supervisor.state_manager import TaskState
from ..supervisor.supervisor import RunReport


def rebuild_run_report(row: AutomationRun) -> RunReport:
    """Reconstruct the RunReport a completed run produced.

    Tolerant of a run that failed before summary_json was ever written (e.g. a
    crash between claiming the run and finishing it) - such a run still has a
    row, just with no results or states to show.
    """
    payload = row.summary_json or {}
    return RunReport(
        run_id=payload.get("run_id", row.id),
        run_date=payload.get("run_date", row.run_date.isoformat()),
        dry_run=payload.get("dry_run", row.dry_run),
        status=payload.get("status", row.status),
        results={
            name: AgentResult.model_validate(value)
            for name, value in payload.get("results", {}).items()
        },
        states={
            name: TaskState(value)
            for name, value in payload.get("states", {}).items()
        },
        errors=payload.get("errors", []),
    )
