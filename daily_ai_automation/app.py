"""Application wiring.

One place that turns Settings into a live engine, session factory and worker
registry, so the CLI, the tests and the scheduler all build the system the
same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config.settings import Settings, load_settings
from .db import create_db_engine, create_session_factory, init_schema
from .logging_setup import configure_logging
from .registry import build_registrations
from .supervisor.supervisor import Supervisor


@dataclass
class Application:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]

    def supervisor(self) -> Supervisor:
        return Supervisor(
            self.settings,
            self.session_factory,
            build_registrations(),
        )


def build_application(
    config_path: Path | None = None,
    *,
    configure_logs: bool = True,
    dry_run_override: bool | None = None,
) -> Application:
    settings = load_settings(config_path)
    if dry_run_override is not None:
        settings = settings.model_copy(update={"dry_run": dry_run_override})

    if configure_logs:
        configure_logging(settings.logging.level, settings.log_path)

    engine = create_db_engine(settings.database_url)
    init_schema(engine)
    return Application(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
    )
