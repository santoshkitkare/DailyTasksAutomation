"""Workflow B: birthday and anniversary greetings (PRD sections 14-20, 44).

Shape of the run:

    Drive -> parse + validate -> today's events -> per recipient:
        generate greeting -> generate image -> compose -> claim -> send -> record

The claim step is the important one. The send-log row is committed *before* the
Gmail send, so a crash between the two leaves a claim in place and the next run
skips the recipient rather than greeting them twice (PRD section 20).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..contracts import (
    AgentResult,
    AgentStatus,
    ContactRecord,
    GreetingContent,
    OccasionOutcome,
    OccasionResult,
)
from ..integrations.email_builder import build_greeting_email, content_hash
from ..integrations.excel import ContactsParseResult, parse_contacts
from ..integrations.gmail import GmailClient
from ..integrations.google_auth import load_credentials
from ..integrations.google_drive import DriveClient
from ..integrations.image_generation import (
    ImageClient,
    ImageGenerationError,
    safe_filename,
)
from ..integrations.llm import LLMClient, PromptLibrary
from ..occasion_dates import ordinal, todays_events, years_elapsed
from ..repositories import DuplicateSendError, OccasionRepository
from ..supervisor.retry import TerminalError
from .base import DailyAgent, RunContext

logger = logging.getLogger(__name__)

PROMPTS = PromptLibrary(Path(__file__).resolve().parent.parent / "prompts")


class OccasionAgent(DailyAgent):
    name = "occasion_agent"

    def __init__(
        self,
        context: RunContext,
        *,
        drive: DriveClient | None = None,
        gmail: GmailClient | None = None,
        llm: LLMClient | None = None,
        images: ImageClient | None = None,
    ) -> None:
        super().__init__(context)
        self._drive = drive
        self._gmail = gmail
        self._llm = llm
        self._images = images
        self._credentials = None

    # -- lazily built clients (injected in tests) --------------------------

    def _google_credentials(self):
        if self._credentials is None:
            settings = self.settings
            self._credentials = load_credentials(
                settings.google.credentials_path(settings.project_root),
                settings.google.token_path(settings.project_root),
                encryption_key=settings.secrets.token_encryption_key,
            )
        return self._credentials

    @property
    def drive(self) -> DriveClient:
        if self._drive is None:
            self._drive = DriveClient(self._google_credentials())
        return self._drive

    @property
    def gmail(self) -> GmailClient:
        if self._gmail is None:
            self._gmail = GmailClient(self._google_credentials())
        return self._gmail

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.settings.secrets.anthropic_api_key)
        return self._llm

    @property
    def images(self) -> ImageClient:
        if self._images is None:
            ai = self.settings.ai
            self._images = ImageClient(
                self.settings.secrets.gemini_api_key,
                model=ai.image_model,
                aspect_ratio=ai.image_aspect_ratio,
                image_size=ai.image_size,
            )
        return self._images

    # -- main --------------------------------------------------------------

    def execute(self) -> AgentResult:
        cfg = self.settings.occasion
        if not cfg.drive_file_id:
            raise TerminalError(
                "occasion.drive_file_id is not set in config.yaml. It is the "
                "long token in the contacts file's Drive share URL, between "
                "/d/ and /edit."
            )

        parsed = self._load_contacts()
        today = self.context.run_date.date()
        due = todays_events(
            parsed.contacts,
            today,
            birthday_enabled=cfg.birthday_enabled,
            anniversary_enabled=cfg.anniversary_enabled,
        )

        warnings = [
            f"Row {row.row_number}: {row.reason}" for row in parsed.invalid
        ]
        warnings.extend(parsed.duplicates)

        logger.info(
            "%s; %d event(s) fall on %s",
            parsed.summary(),
            len(due),
            today.isoformat(),
            extra={"agent": self.name, "count": len(due)},
        )

        if not due:
            return self.result(
                AgentStatus.PARTIAL_SUCCESS if warnings else AgentStatus.COMPLETED,
                summary=f"No birthdays or anniversaries on {today.isoformat()}.",
                items_processed=0,
                warnings=warnings,
                details=self._details([], parsed),
            )

        results = [self._process(contact, today) for contact in due]

        sent = sum(1 for r in results if r.outcome is OccasionOutcome.SENT)
        failed = [r for r in results if r.outcome is OccasionOutcome.FAILED]
        errors = [f"{r.contact.email_address}: {r.reason}" for r in failed]

        if failed and sent:
            status = AgentStatus.PARTIAL_SUCCESS
        elif failed:
            status = AgentStatus.FAILED
        elif warnings:
            status = AgentStatus.PARTIAL_SUCCESS
        else:
            status = AgentStatus.COMPLETED

        return self.result(
            status,
            summary=self._summarise(results),
            items_processed=len(results),
            actions_taken=sent,
            errors=errors,
            warnings=warnings,
            details=self._details(results, parsed),
        )

    # -- steps -------------------------------------------------------------

    def _load_contacts(self) -> ContactsParseResult:
        data = self.drive.download_xlsx(self.settings.occasion.drive_file_id)
        return parse_contacts(data, self.settings.occasion.worksheet)

    def _process(self, contact: ContactRecord, today) -> OccasionResult:
        """One recipient, through the full pre-send gate (PRD section 44)."""
        cfg = self.settings.occasion
        years = years_elapsed(contact.event_date, today)

        if not cfg.send_enabled:
            return OccasionResult(
                contact=contact,
                outcome=OccasionOutcome.SKIPPED_DISABLED,
                reason="occasion.send_enabled is false",
                years=years,
            )

        if not cfg.sender_name.strip():
            # Refusing beats signing a stranger's birthday email "None".
            return OccasionResult(
                contact=contact,
                outcome=OccasionOutcome.FAILED,
                reason=(
                    "occasion.sender_name is empty in config.yaml; refusing to "
                    "send an unsigned greeting"
                ),
                years=years,
            )

        repo = OccasionRepository(self.context.session)
        event_type = str(contact.event_type)
        existing = repo.find(event_type, contact.email_address, today.year)
        if existing is not None:
            return OccasionResult(
                contact=contact,
                outcome=OccasionOutcome.SKIPPED_DUPLICATE,
                reason=f"already logged as {existing.status} on {existing.created_at}",
                years=years,
            )

        try:
            greeting = self._write_greeting(contact, years)
        except Exception as exc:  # noqa: BLE001 - one recipient is not the run
            return self._failure(contact, years, f"greeting generation failed: {exc}")

        image_bytes, image_mime, image_warning = self._make_image(contact, greeting)

        if self.context.dry_run:
            self.audit(
                "SEND_GREETING",
                resource_id=contact.email_address,
                outcome="WOULD_APPLY",
                reason=f"{event_type} on {today.isoformat()}",
                details={"subject": greeting.subject, "image": bool(image_bytes)},
            )
            return OccasionResult(
                contact=contact,
                outcome=OccasionOutcome.SKIPPED_DRY_RUN,
                reason=image_warning or "dry run; nothing sent",
                years=years,
            )

        digest = content_hash(
            greeting.subject, greeting.greeting_line, *greeting.body_paragraphs
        )
        try:
            claim = repo.claim(
                run_id=self.context.run_id,
                event_type=event_type,
                event_date=contact.event_date,
                event_year=today.year,
                recipient_name=contact.full_name,
                recipient_email=contact.email_address,
                content_hash=digest,
            )
        except DuplicateSendError as exc:
            return OccasionResult(
                contact=contact,
                outcome=OccasionOutcome.SKIPPED_DUPLICATE,
                reason=str(exc),
                years=years,
            )

        try:
            message = build_greeting_email(
                to_address=contact.email_address,
                to_name=contact.full_name,
                subject=greeting.subject,
                greeting_line=greeting.greeting_line,
                body_paragraphs=greeting.body_paragraphs,
                closing_line=greeting.closing_line,
                sender_name=cfg.sender_name.strip(),
                image_bytes=image_bytes,
                image_mime=image_mime,
            )
            provider_id = self.gmail.send_message(message)
        except Exception as exc:  # noqa: BLE001 - release the claim and report
            repo.release(claim)
            self.audit(
                "SEND_GREETING",
                resource_id=contact.email_address,
                outcome="FAILED",
                reason=str(exc),
                details={"event_type": event_type},
            )
            return self._failure(contact, years, f"send failed: {exc}")

        repo.mark_sent(claim, provider_id)
        self.audit(
            "SEND_GREETING",
            resource_id=contact.email_address,
            outcome="APPLIED",
            reason=f"{event_type} on {today.isoformat()}",
            details={
                "subject": greeting.subject,
                "provider_message_id": provider_id,
                "image": bool(image_bytes),
            },
        )
        return OccasionResult(
            contact=contact,
            outcome=OccasionOutcome.SENT,
            reason=image_warning,
            provider_message_id=provider_id,
            years=years,
        )

    def _write_greeting(
        self, contact: ContactRecord, years: int | None
    ) -> GreetingContent:
        relationship = (
            str(contact.relationship) if contact.relationship else "Not specified"
        )
        year_line = (
            f"Years completed: {years} (phrase as '{ordinal(years)}')"
            if years and contact.event_type.value == "Anniversary"
            else "Years completed: not supplied - do not reference a number"
        )
        user_content = "\n".join(
            [
                f"Event: {contact.event_type.value}",
                f"Recipient full name: {contact.full_name}",
                f"Recipient first name: {contact.first_name}",
                f"Relationship: {relationship}",
                year_line,
                f"Sender name: {self.settings.occasion.sender_name.strip()}",
            ]
        )
        return self.llm.parse(
            model=self.settings.ai.content_model,
            system=PROMPTS.get("occasion_greeting"),
            user_content=user_content,
            schema=GreetingContent,
            max_tokens=2000,
        )

    def _make_image(
        self, contact: ContactRecord, greeting: GreetingContent
    ) -> tuple[bytes | None, str, str]:
        """Generate the greeting image.

        A missing image degrades the email to text rather than failing the
        send: a birthday greeting that arrives plain is far better than one
        that does not arrive.
        """
        prompt = (
            f"{greeting.image_concept} "
            "Tasteful, elegant, warm colours, celebratory. "
            "No people, no faces, no text other than short decorative wording."
        )
        stem = safe_filename(
            str(contact.event_type), contact.first_name, contact.email_address.split("@")[0]
        )
        destination = self.settings.images_dir / self.context.run_id / stem
        try:
            image = self.images.generate_and_save(prompt, destination)
        except ImageGenerationError as exc:
            logger.warning(
                "Image generation returned nothing for %s: %s",
                contact.email_address,
                exc,
                extra={"agent": self.name},
            )
            return None, "image/png", f"sent without an image ({exc})"
        except Exception as exc:  # noqa: BLE001 - degrade, do not fail the send
            logger.warning(
                "Image generation failed for %s: %s",
                contact.email_address,
                exc,
                extra={"agent": self.name, "error_code": type(exc).__name__},
            )
            return None, "image/png", f"sent without an image ({type(exc).__name__})"
        return image.data, image.mime_type, ""

    def _failure(
        self, contact: ContactRecord, years: int | None, reason: str
    ) -> OccasionResult:
        logger.error(
            "Greeting for %s failed: %s",
            contact.email_address,
            reason,
            extra={"agent": self.name},
        )
        return OccasionResult(
            contact=contact,
            outcome=OccasionOutcome.FAILED,
            reason=reason,
            years=years,
        )

    # -- reporting helpers -------------------------------------------------

    def _details(
        self, results: list[OccasionResult], parsed: ContactsParseResult
    ) -> dict:
        return {
            "results": [r.model_dump(mode="json") for r in results],
            "invalid_rows": [r.model_dump(mode="json") for r in parsed.invalid],
            "duplicate_rows": parsed.duplicates,
            "contacts_total": parsed.total_rows,
            "contacts_valid": len(parsed.contacts),
            "token_usage": self.llm.usage.as_dict() if self._llm else {},
        }

    @staticmethod
    def _summarise(results: list[OccasionResult]) -> str:
        def count(outcome: OccasionOutcome) -> int:
            return sum(1 for r in results if r.outcome is outcome)

        birthdays = sum(1 for r in results if r.contact.event_type.value == "Birthday")
        anniversaries = len(results) - birthdays
        parts = [
            f"{birthdays} birthday(s), {anniversaries} anniversary(ies) today",
            f"{count(OccasionOutcome.SENT)} sent",
        ]
        for outcome, label in (
            (OccasionOutcome.SKIPPED_DUPLICATE, "duplicate(s) skipped"),
            (OccasionOutcome.SKIPPED_DRY_RUN, "held by dry run"),
            (OccasionOutcome.SKIPPED_DISABLED, "skipped (sending disabled)"),
            (OccasionOutcome.FAILED, "failed"),
        ):
            value = count(outcome)
            if value:
                parts.append(f"{value} {label}")
        return ", ".join(parts) + "."
