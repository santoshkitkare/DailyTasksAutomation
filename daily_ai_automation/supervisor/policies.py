"""The action authorization layer (PRD sections 12, 36, 49).

This is the single place where a model's *proposal* becomes an *authorization*.
The classifier can say TO_DELETE about anything it likes; nothing happens until
every rule below passes. Each rule is a separate, independently testable
function so a failure can name the exact reason it refused, which is what makes
the audit trail explainable.

Design rule enforced by review: worker code never calls GmailClient write
methods. It calls decide_to_delete() and hands the verdict to the applier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ..config.settings import GmailSettings
from ..contracts import (
    EmailCategory,
    EmailClassification,
    Priority,
    RecommendedAction,
)
from ..integrations.gmail import GmailMessage


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


def allow(reason: str) -> PolicyDecision:
    return PolicyDecision(Verdict.ALLOW, reason)


def refuse(reason: str) -> PolicyDecision:
    return PolicyDecision(Verdict.REFUSE, reason)


# Categories that always veto an automated action regardless of confidence.
# Losing a bank alert or a security notice to a label is a far worse outcome
# than leaving a newsletter in the inbox, so these are refused outright.
PROTECTED_CATEGORIES = frozenset(
    {
        EmailCategory.FINANCIAL,
        EmailCategory.JOB_OPPORTUNITY,
        EmailCategory.RECRUITER,
        EmailCategory.PERSONAL,
        EmailCategory.WORK,
    }
)

# Content signals that indicate an email may carry account, security, legal or
# money consequences. Matched against subject + snippet only - never the full
# body, which keeps the check cheap and deterministic.
SENSITIVE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bone[- ]?time\s+(pass\s?code|password|pin)\b",
        r"\bOTP\b",
        r"\bverification\s+code\b",
        r"\btwo[- ]factor\b",
        r"\b2FA\b",
        r"\bsecurity\s+alert\b",
        r"\bsuspicious\s+(sign[- ]?in|activity|login)\b",
        r"\bpassword\s+(reset|change[d]?)\b",
        r"\bnew\s+(device|sign[- ]?in)\b",
        # Copular and passive phrasings are common in real alert subjects
        # ("your account is locked", "account has been suspended").
        r"\baccount\s+(?:is\s+|was\s+|has\s+been\s+|will\s+be\s+)?"
        r"(locked|suspended|closed|compromised|disabled|deactivated)\b",
        r"\binvoice\b",
        r"\breceipt\b",
        r"\bpayment\s+(due|failed|received|confirmation)\b",
        r"\bstatement\b",
        r"\btransaction\b",
        r"\brefund\b",
        r"\btax\b",
        r"\blegal\s+notice\b",
        r"\bcontract\b",
        r"\bcourt\b",
        r"\bsubpoena\b",
        r"\bvisa\b",
        r"\bpassport\b",
        r"\bappointment\b",
        r"\binterview\b",
    )
)


def find_sensitive_signal(message: GmailMessage) -> str:
    """Return the first sensitive phrase found, or an empty string."""
    haystack = f"{message.subject}\n{message.snippet}"
    for pattern in SENSITIVE_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return match.group(0)
    return ""


def matched_protected_sender(message: GmailMessage, patterns: list[str]) -> str:
    """Return the protected-sender pattern this message matches, if any."""
    sender = message.sender.casefold()
    for pattern in patterns:
        if pattern and pattern in sender:
            return pattern
    return ""


def decide_to_delete(
    message: GmailMessage,
    classification: EmailClassification,
    settings: GmailSettings,
) -> PolicyDecision:
    """Should the ToDelete label be applied to this message?

    Every check must pass. They are ordered cheapest-and-most-absolute first so
    the recorded reason names the strongest objection rather than an incidental
    one.
    """
    # 1. The model has to have actually asked for it.
    if classification.recommended_action is not RecommendedAction.TO_DELETE:
        return refuse(
            f"recommended action is {classification.recommended_action}, not TO_DELETE"
        )

    # 2. Untrusted input tried to steer the automation - trust nothing about it.
    if classification.contains_injection_attempt:
        return refuse("message content contains an apparent prompt-injection attempt")

    # 3. Anything the user might have to answer stays put.
    if classification.requires_reply:
        return refuse("message is marked as requiring a reply")

    # 4. Priority and category vetoes.
    if classification.priority.rank <= Priority.MEDIUM.rank:
        return refuse(f"priority is {classification.priority}, above the LOW threshold")

    if classification.category in PROTECTED_CATEGORIES:
        return refuse(f"category {classification.category} is protected")

    if str(classification.category) not in settings.auto_label_allowed_categories:
        return refuse(
            f"category {classification.category} is not in "
            f"auto_label_allowed_categories"
        )

    # 5. Confidence threshold (PRD section 12).
    if classification.confidence < settings.auto_label_threshold:
        return refuse(
            f"confidence {classification.confidence:.2f} is below the "
            f"{settings.auto_label_threshold:.2f} threshold"
        )

    # 6. Sender and content vetoes.
    protected = matched_protected_sender(message, settings.protected_senders)
    if protected:
        return refuse(f"sender matches protected pattern {protected!r}")

    signal = find_sensitive_signal(message)
    if signal:
        return refuse(f"subject or preview mentions {signal!r}")

    # 7. Never act on something the user already engaged with.
    if "STARRED" in message.label_ids or "IMPORTANT" in message.label_ids:
        return refuse("message is starred or marked important by Gmail")

    return allow(
        f"{classification.category} at confidence "
        f"{classification.confidence:.2f} with no reply required"
    )


def should_notify(classification: EmailClassification) -> bool:
    """Does this email belong in the daily digest?

    Deliberately broader than the labeling rule: over-reporting costs the user
    a few lines of reading, while under-reporting loses a job opportunity.
    """
    return (
        classification.requires_reply
        or classification.priority.rank <= Priority.MEDIUM.rank
        or classification.category
        in (EmailCategory.JOB_OPPORTUNITY, EmailCategory.RECRUITER)
        or classification.contains_injection_attempt
    )
