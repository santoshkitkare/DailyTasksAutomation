"""Writing and sending the daily report.

This runs after the supervisor loop rather than as a registered worker: it
needs the whole RunReport, including the outcome of every worker, which no
worker is allowed to see. Keeping it outside the registry also means a failure
to deliver the report can never change a run's status.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config.settings import Settings
from ..integrations.email_builder import build_report_email
from ..integrations.gmail import GmailClient
from ..integrations.google_auth import load_credentials
from ..supervisor.supervisor import RunReport
from .digest import RenderedReport, render

logger = logging.getLogger(__name__)


def write_report_file(settings: Settings, report: RunReport, rendered: RenderedReport) -> Path:
    """Always keep a local copy, even when the email fails to send."""
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / f"{report.run_date}-{report.run_id}.html"
    path.write_text(rendered.html, encoding="utf-8")
    return path


def deliver(
    settings: Settings,
    report: RunReport,
    *,
    gmail: GmailClient | None = None,
) -> tuple[Path, str]:
    """Render, save to disk, and email the report.

    Returns the file path and a human-readable delivery status. Never raises:
    the report is the last step of the run and a mail failure must not mask a
    successful run, only be reported.
    """
    rendered = render(report)
    path = write_report_file(settings, report, rendered)

    if not settings.notifications.enabled:
        return path, "notifications disabled in config"

    if report.dry_run:
        # The digest is a read-only summary, but sending it in dry-run mode
        # would contradict the promise that a dry run sends no mail at all.
        return path, "dry run - report written to disk, not emailed"

    try:
        client = gmail or _build_gmail(settings)
        recipient = settings.notifications.recipient.strip() or client.address
        message = build_report_email(
            to_address=recipient,
            subject=rendered.subject,
            html_body=rendered.html,
            text_body=rendered.text,
        )
        client.send_message(message)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.error(
            "Could not email the daily report: %s",
            exc,
            extra={"run_id": report.run_id, "error_code": type(exc).__name__},
        )
        return path, f"email delivery failed ({type(exc).__name__}: {exc})"

    return path, f"emailed to {recipient}"


def _build_gmail(settings: Settings) -> GmailClient:
    credentials = load_credentials(
        settings.google.credentials_path(settings.project_root),
        settings.google.token_path(settings.project_root),
        encryption_key=settings.secrets.token_encryption_key,
    )
    return GmailClient(credentials)
