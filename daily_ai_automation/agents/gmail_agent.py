"""Workflow A: Gmail triage (PRD section 9).

Shape of the run:

    window -> search -> drop already-processed -> fetch -> classify (LLM)
           -> policy engine -> apply or refuse -> record -> structured result

The LLM appears at exactly one step, and its output is never used as an
instruction: it is data fed into ``decide_to_delete``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..contracts import (
    AgentResult,
    AgentStatus,
    EmailClassification,
    EmailClassificationBatch,
    TriagedEmail,
)
from ..integrations.gmail import GmailClient, GmailMessage, is_not_found
from ..integrations.google_auth import load_credentials
from ..integrations.llm import LLMClient, PromptLibrary, wrap_untrusted
from ..repositories import EmailRepository, RunRepository
from ..supervisor.policies import decide_to_delete, should_notify
from .base import DailyAgent, RunContext

logger = logging.getLogger(__name__)

PROMPTS = PromptLibrary(Path(__file__).resolve().parent.parent / "prompts")


class GmailTriageAgent(DailyAgent):
    name = "gmail_triage_agent"

    def __init__(
        self,
        context: RunContext,
        *,
        gmail: GmailClient | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        super().__init__(context)
        self._gmail = gmail
        self._llm = llm

    # -- lazily built clients (injected in tests) --------------------------

    @property
    def gmail(self) -> GmailClient:
        if self._gmail is None:
            settings = self.settings
            credentials = load_credentials(
                settings.google.credentials_path(settings.project_root),
                settings.google.token_path(settings.project_root),
                encryption_key=settings.secrets.token_encryption_key,
            )
            self._gmail = GmailClient(credentials)
        return self._gmail

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.settings.secrets.anthropic_api_key)
        return self._llm

    # -- main --------------------------------------------------------------

    def execute(self) -> AgentResult:
        cfg = self.settings.gmail
        warnings: list[str] = []
        errors: list[str] = []

        since = self._window_start()
        query = self._build_query(since)
        logger.info(
            "Searching Gmail since %s", since.isoformat(), extra={"agent": self.name}
        )

        candidate_ids = self.gmail.search_message_ids(
            query, max_results=cfg.max_emails_per_run
        )
        repo = EmailRepository(self.context.session)
        seen = repo.already_processed(candidate_ids)
        new_ids = [mid for mid in candidate_ids if mid not in seen]

        if len(candidate_ids) == cfg.max_emails_per_run:
            warnings.append(
                f"Hit the max_emails_per_run cap of {cfg.max_emails_per_run}; "
                "older messages in the window were not examined."
            )

        logger.info(
            "%d message(s) matched, %d already processed, %d to classify",
            len(candidate_ids),
            len(seen),
            len(new_ids),
            extra={"agent": self.name, "count": len(new_ids)},
        )

        if not new_ids:
            return self.result(
                AgentStatus.COMPLETED,
                summary="No new emails to process.",
                details={"window_start": since.isoformat(), "notify": []},
                warnings=warnings,
            )

        messages, fetch_warnings = self._fetch(new_ids)
        warnings.extend(fetch_warnings)
        if not messages:
            return self.result(
                AgentStatus.PARTIAL_SUCCESS if warnings else AgentStatus.COMPLETED,
                summary="No messages could be fetched.",
                details={"window_start": since.isoformat(), "notify": []},
                warnings=warnings,
            )

        classifications, classify_errors = self._classify(messages)
        errors.extend(classify_errors)

        triaged: list[TriagedEmail] = []
        actions = 0
        for message in messages:
            classification = classifications.get(message.message_id)
            if classification is None:
                warnings.append(
                    f"No classification returned for message {message.message_id}"
                )
                continue
            email = self._apply(message, classification)
            # A FAILED attempt is not an action taken; counting it would
            # overstate what the run actually changed.
            if email.action_applied in ("LABELED", "WOULD_LABEL"):
                actions += 1
            triaged.append(email)

        repo.record_many(self.context.run_id, triaged)

        notify = [e for e in triaged if should_notify(e.classification)]
        status = (
            AgentStatus.PARTIAL_SUCCESS
            if errors or warnings
            else AgentStatus.COMPLETED
        )
        return self.result(
            status,
            summary=self._summarise(triaged, actions),
            items_processed=len(triaged),
            actions_taken=actions,
            errors=errors,
            warnings=warnings,
            details={
                "window_start": since.isoformat(),
                "notify": [e.model_dump(mode="json") for e in notify],
                "counts": self._counts(triaged),
                "token_usage": self.llm.usage.as_dict(),
                # Recorded so analytics can price historical runs correctly
                # even after the configured model changes later.
                "model": self.settings.ai.classification_model,
            },
        )

    # -- steps -------------------------------------------------------------

    def _window_start(self) -> datetime:
        """When to read from.

        The start of the last *successful* Gmail execution, minus a small
        overlap so a message that arrived while the previous run was mid-flight
        is not skipped. The message-ID dedup makes the overlap free.
        """
        cfg = self.settings.gmail
        repo = RunRepository(self.context.session)
        last = repo.last_successful_started_at(self.name)

        if last is None:
            logger.info(
                "No previous successful Gmail run; using the %d-day first-run window",
                cfg.first_run_lookback_days,
                extra={"agent": self.name},
            )
            return datetime.now(timezone.utc) - timedelta(
                days=cfg.first_run_lookback_days
            )

        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return last - timedelta(minutes=10)

    def _build_query(self, since: datetime) -> str:
        # Gmail's `after:` takes whole seconds since the epoch.
        parts = [f"after:{int(since.timestamp())}"]
        if self.settings.gmail.base_query:
            parts.append(self.settings.gmail.base_query)
        return " ".join(parts)

    def _fetch(self, ids: list[str]) -> tuple[list[GmailMessage], list[str]]:
        messages: list[GmailMessage] = []
        warnings: list[str] = []
        for message_id in ids:
            try:
                messages.append(
                    self.gmail.get_message(
                        message_id, max_body_chars=self.settings.ai.max_body_chars
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad message is not fatal
                if is_not_found(exc):
                    warnings.append(f"Message {message_id} disappeared before fetch")
                    continue
                raise
        return messages, warnings

    def _classify(
        self, messages: list[GmailMessage]
    ) -> tuple[dict[str, EmailClassification], list[str]]:
        """Classify in batches. A failed batch degrades that batch only."""
        system = PROMPTS.get("gmail_triage")
        size = self.settings.ai.classification_batch_size
        results: dict[str, EmailClassification] = {}
        errors: list[str] = []

        for start in range(0, len(messages), size):
            batch = messages[start : start + size]
            try:
                parsed = self.llm.parse(
                    model=self.settings.ai.classification_model,
                    system=system,
                    user_content=_render_batch(batch),
                    schema=EmailClassificationBatch,
                )
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                logger.error(
                    "Classification batch starting at %d failed: %s",
                    start,
                    exc,
                    extra={"agent": self.name, "error_code": type(exc).__name__},
                )
                errors.append(f"Classification failed for {len(batch)} message(s): {exc}")
                continue

            known = {m.message_id for m in batch}
            for classification in parsed.classifications:
                # A hallucinated or duplicated ID is dropped rather than
                # allowed to attach a verdict to the wrong message.
                if classification.message_id in known:
                    results[classification.message_id] = classification
                else:
                    errors.append(
                        "Model returned a classification for unknown message "
                        f"{classification.message_id!r}; discarded."
                    )
        return results, errors

    def _apply(
        self, message: GmailMessage, classification: EmailClassification
    ) -> TriagedEmail:
        """Run the policy engine and, if it allows, perform the Gmail write."""
        email = TriagedEmail(
            message_id=message.message_id,
            thread_id=message.thread_id,
            sender=message.sender,
            subject=message.subject,
            received_at=message.received_at,
            classification=classification,
        )

        decision = decide_to_delete(message, classification, self.settings.gmail)
        if not decision.allowed:
            email.action_applied = "NONE"
            email.action_reason = decision.reason
            # Only audit refusals the model actually asked for; otherwise every
            # newsletter produces a noise row.
            if str(classification.recommended_action) == "TO_DELETE":
                self.audit(
                    "ADD_LABEL",
                    resource_id=message.message_id,
                    outcome="REFUSED",
                    reason=decision.reason,
                    confidence=classification.confidence,
                    details={"label": self.settings.gmail.delete_label},
                )
            return email

        label = self.settings.gmail.delete_label
        if self.context.dry_run:
            email.action_applied = "WOULD_LABEL"
            email.action_reason = decision.reason
            self.audit(
                "ADD_LABEL",
                resource_id=message.message_id,
                outcome="WOULD_APPLY",
                reason=decision.reason,
                confidence=classification.confidence,
                details={"label": label, "subject": message.subject},
            )
            return email

        try:
            label_id = self.gmail.ensure_label(label)
            self.gmail.add_label(message.message_id, label_id)
        except Exception as exc:  # noqa: BLE001 - one label failure is not fatal
            email.action_applied = "FAILED"
            email.action_reason = str(exc)
            self.audit(
                "ADD_LABEL",
                resource_id=message.message_id,
                outcome="FAILED",
                reason=str(exc),
                confidence=classification.confidence,
                details={"label": label},
            )
            return email

        email.action_applied = "LABELED"
        email.action_reason = decision.reason
        self.audit(
            "ADD_LABEL",
            resource_id=message.message_id,
            outcome="APPLIED",
            reason=decision.reason,
            confidence=classification.confidence,
            details={"label": label, "subject": message.subject},
        )
        return email

    # -- reporting helpers -------------------------------------------------

    @staticmethod
    def _counts(triaged: list[TriagedEmail]) -> dict:
        counts = {
            "processed": len(triaged),
            "job_opportunities": 0,
            "requires_reply": 0,
            "high_priority": 0,
            "labeled": 0,
            "would_label": 0,
            "injection_attempts": 0,
            "by_category": {},
        }
        for email in triaged:
            c = email.classification
            category = str(c.category)
            counts["by_category"][category] = counts["by_category"].get(category, 0) + 1
            if category in ("JOB_OPPORTUNITY", "RECRUITER"):
                counts["job_opportunities"] += 1
            if c.requires_reply:
                counts["requires_reply"] += 1
            if str(c.priority) in ("CRITICAL", "HIGH"):
                counts["high_priority"] += 1
            if c.contains_injection_attempt:
                counts["injection_attempts"] += 1
            if email.action_applied == "LABELED":
                counts["labeled"] += 1
            elif email.action_applied == "WOULD_LABEL":
                counts["would_label"] += 1
        return counts

    def _summarise(self, triaged: list[TriagedEmail], actions: int) -> str:
        counts = self._counts(triaged)
        verb = "would be labeled" if self.context.dry_run else "labeled"
        labeled = counts["would_label"] if self.context.dry_run else counts["labeled"]
        return (
            f"Processed {counts['processed']} new emails. "
            f"Found {counts['job_opportunities']} job/recruiter emails, "
            f"{counts['requires_reply']} needing a reply, "
            f"{labeled} {verb} {self.settings.gmail.delete_label}."
        )


def _render_batch(messages: list[GmailMessage]) -> str:
    """Build the user turn: metadata as trusted framing, body as fenced data."""
    blocks = []
    for message in messages:
        blocks.append(
            "\n".join(
                [
                    f"### message_id: {message.message_id}",
                    f"From: {message.sender}",
                    f"Subject: {message.subject}",
                    f"Received: {message.received_at.isoformat()}",
                    "",
                    wrap_untrusted(message.body or message.snippet),
                ]
            )
        )
    header = (
        f"Classify the following {len(messages)} email(s). Return exactly "
        f"{len(messages)} classification objects, one per message_id, in order."
    )
    return header + "\n\n" + "\n\n---\n\n".join(blocks)
