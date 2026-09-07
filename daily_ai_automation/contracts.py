"""Pydantic contracts shared between the supervisor, the workers and the LLM.

These are the *only* shapes an LLM is allowed to produce. Every field is a
closed enum or a scalar, so the model has no vocabulary in which to request an
arbitrary API operation (PRD section 35/49). Whether an action actually happens
is decided later, by the policy engine, from these values.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


# --------------------------------------------------------------------------
# Agent lifecycle (PRD section 8)
# --------------------------------------------------------------------------


class AgentStatus(StrEnum):
    STARTED = "STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self not in (AgentStatus.STARTED, AgentStatus.IN_PROGRESS)

    @property
    def is_success(self) -> bool:
        return self in (AgentStatus.COMPLETED, AgentStatus.PARTIAL_SUCCESS)


class AgentResult(BaseModel):
    """What every worker hands back to the supervisor."""

    run_id: str
    agent: str
    status: AgentStatus
    started_at: datetime
    completed_at: datetime | None = None
    items_processed: int = 0
    actions_taken: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    # Worker-specific payload the reporter renders. Never persisted raw to logs.
    details: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Gmail triage (PRD section 9.3)
# --------------------------------------------------------------------------


class EmailCategory(StrEnum):
    JOB_OPPORTUNITY = "JOB_OPPORTUNITY"
    RECRUITER = "RECRUITER"
    WORK = "WORK"
    PERSONAL = "PERSONAL"
    FINANCIAL = "FINANCIAL"
    NEWSLETTER = "NEWSLETTER"
    PROMOTION = "PROMOTION"
    SOCIAL = "SOCIAL"
    NOTIFICATION = "NOTIFICATION"
    SPAM_LIKE = "SPAM_LIKE"
    OTHER = "OTHER"


class Priority(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

    @property
    def rank(self) -> int:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4}
        return order[self.value]


class RecommendedAction(StrEnum):
    REPLY = "REPLY"
    READ = "READ"
    REVIEW = "REVIEW"
    ARCHIVE = "ARCHIVE"
    TO_DELETE = "TO_DELETE"
    NO_ACTION = "NO_ACTION"


class EmailClassification(BaseModel):
    """The LLM's judgement about one message. A proposal, not a decision."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    category: EmailCategory
    requires_reply: bool
    priority: Priority
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    why_it_matters: str = ""
    recommended_action: RecommendedAction
    reason: str
    suggested_deadline: str = ""
    suggested_reply: str = ""
    # Set by the model when the body appears to contain instructions aimed at
    # the automation itself. Vetoes every automated action (PRD section 35).
    contains_injection_attempt: bool = False


class EmailClassificationBatch(BaseModel):
    """Envelope so one request can classify many messages."""

    model_config = ConfigDict(extra="forbid")

    classifications: list[EmailClassification]


class TriagedEmail(BaseModel):
    """A classification joined back to the message metadata the reporter needs."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    received_at: datetime
    classification: EmailClassification
    action_applied: str = "NONE"
    action_reason: str = ""

    @computed_field
    @property
    def gmail_link(self) -> str:
        """Deep link into the thread.

        A computed field, not a plain property: the reporter reads this from
        the serialised dict, so it has to survive model_dump().
        """
        return f"https://mail.google.com/mail/u/0/#inbox/{self.thread_id}"


# --------------------------------------------------------------------------
# Occasions (PRD sections 14, 16, 17)
# --------------------------------------------------------------------------


class EventType(StrEnum):
    BIRTHDAY = "Birthday"
    ANNIVERSARY = "Anniversary"


class Relationship(StrEnum):
    FAMILY = "Family"
    CLOSE_FRIEND = "Close Friend"
    FRIEND = "Friend"
    COLLEAGUE = "Colleague"
    CLIENT = "Client"

    @classmethod
    def parse(cls, value: str | None) -> "Relationship | None":
        """Lenient parse — an unrecognised value falls back to the default tone."""
        if not value:
            return None
        normalised = " ".join(str(value).split()).casefold()
        for member in cls:
            if member.value.casefold() == normalised:
                return member
        return None


class ContactRecord(BaseModel):
    """One validated row of the contacts spreadsheet."""

    row_number: int
    event_type: EventType
    event_date: date
    full_name: str
    email_address: str
    mobile_number: str = ""
    relationship: Relationship | None = None

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name.split() else self.full_name


class InvalidContactRow(BaseModel):
    """A row that failed validation. Reported, never processed (PRD section 43)."""

    row_number: int
    reason: str
    raw: dict


class GreetingContent(BaseModel):
    """The LLM's greeting. Text only — it never chooses a recipient."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    greeting_line: str
    body_paragraphs: list[str]
    closing_line: str
    image_concept: str = Field(
        description="A short, generic scene description for the image model. "
        "Must contain no personal information beyond a first name."
    )


class OccasionOutcome(StrEnum):
    SENT = "SENT"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_DRY_RUN = "SKIPPED_DRY_RUN"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"
    FAILED = "FAILED"


class OccasionResult(BaseModel):
    contact: ContactRecord
    outcome: OccasionOutcome
    reason: str = ""
    provider_message_id: str = ""
    years: int | None = None
