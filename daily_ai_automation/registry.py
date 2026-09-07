"""The list of workers the supervisor runs, in order.

Adding a future daily agent (calendar, expenses, news - PRD section 50) means
appending one AgentRegistration here and one enabled flag in config.yaml.
Nothing else changes.
"""

from __future__ import annotations

from .config.settings import Settings
from .supervisor.supervisor import AgentRegistration


def build_registrations() -> list[AgentRegistration]:
    from .agents.gmail_agent import GmailTriageAgent
    from .agents.occasion_agent import OccasionAgent

    return [
        AgentRegistration(
            name=GmailTriageAgent.name,
            factory=GmailTriageAgent,
            is_enabled=lambda s: s.gmail.enabled,
        ),
        AgentRegistration(
            name=OccasionAgent.name,
            factory=OccasionAgent,
            is_enabled=_occasion_enabled,
        ),
    ]


def _occasion_enabled(settings: Settings) -> bool:
    """The occasion worker is pointless without at least one event type on."""
    return settings.occasion.enabled and (
        settings.occasion.birthday_enabled or settings.occasion.anniversary_enabled
    )
