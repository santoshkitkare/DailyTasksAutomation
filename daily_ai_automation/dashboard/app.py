"""The local dashboard: report browsing, open-action tracking, analytics.

Read-only against the automation database except for one mutation - marking an
action item done or dismissed - which never touches Gmail. Runs entirely on
localhost; there is no authentication layer because there is nothing here that
reaches beyond this machine. If this is ever exposed past localhost, add auth
first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.status import HTTP_303_SEE_OTHER

from ..config.settings import Settings
from ..db import session_scope
from ..repositories import (
    AuditRepository,
    EmailRepository,
    OccasionRepository,
    RunRepository,
    UnknownMessageError,
)
from ..repositories.email_repository import ACTIONABLE_CATEGORIES, USER_ACTION_STATUSES
from ..reporting.reconstruct import rebuild_run_report
from .analytics import build_analytics

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(settings: Settings, session_factory: sessionmaker[Session]) -> FastAPI:
    app = FastAPI(title="Daily AI Supervisor Dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def render(request: Request, template: str, active: str, **context) -> HTMLResponse:
        with session_scope(session_factory) as session:
            open_action_count = len(EmailRepository(session).open_actions(limit=1000))
        return templates.TemplateResponse(
            request,
            template,
            {
                "active": active,
                "open_action_count": open_action_count,
                "flash": request.query_params.get("flash"),
                **context,
            },
        )

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/runs")

    @app.get("/runs", response_class=HTMLResponse)
    def runs_list(request: Request, limit: int = 60) -> HTMLResponse:
        with session_scope(session_factory) as session:
            runs = RunRepository(session).recent(limit)
        return render(request, "runs_list.html", "runs", runs=runs)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        with session_scope(session_factory) as session:
            row = RunRepository(session).get(run_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"No run {run_id!r}")
            report = rebuild_run_report(row)
            emails = EmailRepository(session).for_run(run_id)
            occasions = OccasionRepository(session).for_run(run_id)
            audit_entries = AuditRepository(session).for_run(run_id)

        actionable_emails = [
            e
            for e in emails
            if e.user_action_status == "PENDING"
            and (e.requires_reply or e.category in ACTIONABLE_CATEGORIES)
        ]
        return render(
            request,
            "run_detail.html",
            "runs",
            report=report,
            emails=emails,
            actionable_emails=actionable_emails,
            occasions=occasions,
            audit_entries=audit_entries,
        )

    @app.get("/actions", response_class=HTMLResponse)
    def open_actions(request: Request) -> HTMLResponse:
        with session_scope(session_factory) as session:
            actions = EmailRepository(session).open_actions()
        return render(request, "actions.html", "actions", actions=actions)

    @app.post("/actions/{message_id}/mark")
    def mark_action(
        message_id: str,
        status: Annotated[str, Form()],
        redirect_to: Annotated[str, Form()] = "/actions",
        note: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        if status not in USER_ACTION_STATUSES or status == "PENDING":
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {[s for s in USER_ACTION_STATUSES if s != 'PENDING']}",
            )
        with session_scope(session_factory) as session:
            try:
                EmailRepository(session).mark_action(message_id, status, note=note)
            except UnknownMessageError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Only ever redirect within this app - redirect_to is a same-origin,
        # server-controlled value (a form field we render ourselves), but it is
        # still resubmitted by the browser, so it is validated as a path
        # rather than trusted as an arbitrary URL.
        destination = redirect_to if redirect_to.startswith("/") else "/actions"
        target = f"{destination}?flash=Marked+{status.lower()}"
        return RedirectResponse(target, status_code=HTTP_303_SEE_OTHER)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request, days: int = 30) -> HTMLResponse:
        with session_scope(session_factory) as session:
            summary = build_analytics(session, limit=days)
        return render(request, "analytics.html", "analytics", summary=summary)

    return app
