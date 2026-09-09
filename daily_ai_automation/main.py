"""Command-line entry point.

    daily-automation setup     preflight every prerequisite, report what is missing
    daily-automation auth      run the Google OAuth consent flow
    daily-automation run       execute the daily supervisor cycle
    daily-automation history   list recent runs
    daily-automation report    re-render a past run's report
    daily-automation dashboard launch the local web dashboard
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .app import build_application
from .config.settings import PROJECT_ROOT, load_settings
from .integrations.google_auth import GoogleAuthError, load_credentials
from .logging_setup import configure_logging
from .reporting.delivery import deliver
from .supervisor.state_manager import DuplicateRunError

app = typer.Typer(
    add_completion=False,
    help="Daily AI Supervisor for Gmail triage and occasion greetings.",
)
console = Console()
logger = logging.getLogger(__name__)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.yaml.", show_default=False),
]

OK = "[green]OK[/green]"
MISSING = "[red]MISSING[/red]"
WARN = "[yellow]CHECK[/yellow]"


@app.command()
def run(
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Override config.yaml. Dry run performs no Gmail writes and "
            "sends no mail.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing run for today's date."),
    ] = False,
) -> None:
    """Execute one daily supervisor cycle."""
    application = build_application(config, dry_run_override=dry_run)

    try:
        report = application.supervisor().run(force=force)
    except DuplicateRunError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=2) from exc

    path, delivery_status = deliver(application.settings, report)

    console.print()
    console.print(_status_line(report.status))
    for name, result in report.results.items():
        console.print(f"  {name}: [bold]{result.status}[/bold] - {result.summary}")
    for name, state in report.states.items():
        if name not in report.results:
            console.print(f"  {name}: [dim]{state}[/dim]")
    console.print(f"\nReport: {path}")
    console.print(f"Delivery: {delivery_status}")

    if report.status == "FAILED":
        raise typer.Exit(code=1)


@app.command()
def auth(config: ConfigOption = None) -> None:
    """Authorise Google access. Opens a browser once."""
    settings = load_settings(config)
    configure_logging(settings.logging.level, settings.log_path)
    try:
        load_credentials(
            settings.google.credentials_path(settings.project_root),
            settings.google.token_path(settings.project_root),
            encryption_key=settings.secrets.token_encryption_key,
            allow_interactive=True,
        )
    except GoogleAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green]Google authorisation complete.[/green]")


@app.command()
def setup(config: ConfigOption = None) -> None:
    """Check every prerequisite and report exactly what is missing."""
    try:
        settings = load_settings(config)
    except Exception as exc:  # noqa: BLE001 - config errors are the point here
        console.print(f"[red]Configuration could not be loaded:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Preflight", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    blocking = 0

    def add(name: str, ok: bool, detail: str, *, blocks: bool = True) -> None:
        nonlocal blocking
        if ok:
            table.add_row(name, OK, detail)
            return
        table.add_row(name, MISSING if blocks else WARN, detail)
        if blocks:
            blocking += 1

    creds_path = settings.google.credentials_path(settings.project_root)
    add(
        "OAuth client",
        creds_path.exists(),
        str(creds_path)
        if creds_path.exists()
        else f"Create a Desktop app OAuth client and save it as {creds_path}",
    )

    token_path = settings.google.token_path(settings.project_root)
    add(
        "Google token",
        token_path.exists(),
        str(token_path) if token_path.exists() else "Run: daily-automation auth",
    )

    add(
        "ANTHROPIC_API_KEY",
        bool(settings.secrets.anthropic_api_key),
        "set" if settings.secrets.anthropic_api_key else "Add it to .env",
    )
    add(
        "GEMINI_API_KEY",
        bool(settings.secrets.gemini_api_key),
        "set" if settings.secrets.gemini_api_key else "Add it to .env",
        blocks=settings.occasion.enabled,
    )
    add(
        "Token encryption",
        bool(settings.secrets.token_encryption_key),
        "enabled"
        if settings.secrets.token_encryption_key
        else "TOKEN_ENCRYPTION_KEY unset; the token is stored unencrypted",
        blocks=False,
    )

    add(
        "occasion.drive_file_id",
        bool(settings.occasion.drive_file_id),
        settings.occasion.drive_file_id or "Set it in config.yaml",
        blocks=settings.occasion.enabled,
    )
    add(
        "occasion.sender_name",
        bool(settings.occasion.sender_name.strip()),
        settings.occasion.sender_name
        or "Set it in config.yaml; greetings will not send while it is empty",
        blocks=settings.occasion.enabled and settings.occasion.send_enabled,
    )

    # Live checks: only attempted once the credentials are actually present.
    if creds_path.exists() and token_path.exists():
        _live_checks(settings, add)

    console.print(table)

    if settings.dry_run:
        console.print(
            "\n[yellow]dry_run is true[/yellow] - runs will classify and report "
            "but will not write to Gmail or send mail."
        )

    if blocking:
        console.print(f"\n[red]{blocking} blocking item(s) outstanding.[/red]")
        raise typer.Exit(code=1)
    console.print("\n[green]All checks passed.[/green]")


def _live_checks(settings, add) -> None:  # noqa: ANN001 - local callback
    from .integrations.gmail import GmailClient
    from .integrations.google_drive import DriveClient, DriveFileError

    try:
        credentials = load_credentials(
            settings.google.credentials_path(settings.project_root),
            settings.google.token_path(settings.project_root),
            encryption_key=settings.secrets.token_encryption_key,
        )
    except GoogleAuthError as exc:
        add("Google credentials", False, str(exc))
        return

    try:
        address = GmailClient(credentials).address
        add("Gmail access", True, f"authenticated as {address}")
    except Exception as exc:  # noqa: BLE001 - surfaced in the table
        add("Gmail access", False, f"{type(exc).__name__}: {exc}")

    if not settings.occasion.drive_file_id:
        return
    try:
        meta = DriveClient(credentials).describe(settings.occasion.drive_file_id)
        add("Contacts file", True, f"{meta.name} ({meta.size} bytes)")
    except DriveFileError as exc:
        add("Contacts file", False, str(exc), blocks=settings.occasion.enabled)
    except Exception as exc:  # noqa: BLE001 - surfaced in the table
        add("Contacts file", False, f"{type(exc).__name__}: {exc}")


@app.command()
def history(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option(help="How many runs to show.")] = 10,
) -> None:
    """List recent runs."""
    from .db import session_scope
    from .repositories import RunRepository

    application = build_application(config, configure_logs=False)
    table = Table(title="Recent runs")
    for column in ("Run ID", "Date", "Status", "Mode", "Summary"):
        table.add_column(column, overflow="fold")

    with session_scope(application.session_factory) as session:
        for row in RunRepository(session).recent(limit):
            table.add_row(
                row.id,
                row.run_date.isoformat(),
                row.status,
                "dry run" if row.dry_run else "live",
                row.summary or "",
            )
    console.print(table)


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run ID, or 'last'.")] = "last",
    config: ConfigOption = None,
) -> None:
    """Print the text report for a past run."""
    from .db import session_scope
    from .repositories import RunRepository
    from .reporting.digest import render
    from .reporting.reconstruct import rebuild_run_report

    application = build_application(config, configure_logs=False)
    with session_scope(application.session_factory) as session:
        repo = RunRepository(session)
        if run_id == "last":
            recent = repo.recent(1)
            row = recent[0] if recent else None
        else:
            row = repo.get(run_id)
        if row is None:
            console.print(
                "[yellow]No runs recorded yet.[/yellow]"
                if run_id == "last"
                else f"[red]No run with ID {run_id}[/red]"
            )
            raise typer.Exit(code=1)
        rebuilt = rebuild_run_report(row)

    console.print(render(rebuilt).text)


@app.command()
def dashboard(
    config: ConfigOption = None,
    host: Annotated[
        str, typer.Option(help="Bind address. Localhost only unless you know why not.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to serve on.")] = 8787,
) -> None:
    """Launch the local web dashboard: run history, open actions, analytics.

    Read-only against the automation database except for marking an action
    item done or dismissed, which never writes to Gmail. There is no
    authentication - keep --host at 127.0.0.1 unless this machine's network
    exposure has been thought through.
    """
    import uvicorn

    from .dashboard import create_app

    application = build_application(config, configure_logs=True)
    web_app = create_app(application.settings, application.session_factory)

    console.print(f"Dashboard running at [bold]http://{host}:{port}[/bold]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


def _status_line(status: str) -> str:
    colour = {
        "COMPLETED": "green",
        "PARTIAL_SUCCESS": "yellow",
        "FAILED": "red",
    }.get(status, "white")
    return f"Overall status: [{colour}][bold]{status}[/bold][/{colour}]"


def main() -> None:  # pragma: no cover - console entry point
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
