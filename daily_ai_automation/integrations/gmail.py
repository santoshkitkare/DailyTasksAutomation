"""Gmail API access.

This module is the *only* place that talks to Gmail. The write methods
(``add_label``, ``send_message``) are called exclusively by the policy engine
and the pre-send gate - never directly by a worker and never in response to
something an LLM asked for (PRD sections 36, 49).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Strip scripts/styles before the visible-text pass, otherwise their contents
# survive tag removal and dominate the text we send to the model.
_SCRIPT_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class GmailMessage:
    """The subset of a Gmail message the triage worker is allowed to see."""

    message_id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    received_at: datetime
    snippet: str
    body: str
    label_ids: list[str] = field(default_factory=list)

    @property
    def sender_email(self) -> str:
        match = re.search(r"<([^>]+)>", self.sender)
        return (match.group(1) if match else self.sender).strip().lower()


class GmailClient:
    def __init__(self, credentials) -> None:  # noqa: ANN001 - google Credentials
        self._service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        self._label_cache: dict[str, str] | None = None

    # -- profile -----------------------------------------------------------

    def get_profile(self) -> dict:
        return self._service.users().getProfile(userId="me").execute()

    @property
    def address(self) -> str:
        return self.get_profile().get("emailAddress", "")

    # -- reading -----------------------------------------------------------

    def search_message_ids(self, query: str, *, max_results: int) -> list[str]:
        """List message IDs matching a Gmail search query, newest first."""
        ids: list[str] = []
        page_token = None
        service = self._service.users().messages()
        while len(ids) < max_results:
            response = service.list(
                userId="me",
                q=query,
                maxResults=min(500, max_results - len(ids)),
                pageToken=page_token,
            ).execute()
            ids.extend(item["id"] for item in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_results]

    def get_message(self, message_id: str, *, max_body_chars: int) -> GmailMessage:
        raw = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return _parse_message(raw, max_body_chars=max_body_chars)

    # -- labels ------------------------------------------------------------

    def _labels(self) -> dict[str, str]:
        if self._label_cache is None:
            response = self._service.users().labels().list(userId="me").execute()
            self._label_cache = {
                label["name"]: label["id"] for label in response.get("labels", [])
            }
        return self._label_cache

    def label_id(self, name: str) -> str | None:
        return self._labels().get(name)

    def ensure_label(self, name: str) -> str:
        """Return the label ID, creating the label if it does not exist."""
        existing = self.label_id(name)
        if existing:
            return existing
        created = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        logger.info("Created Gmail label %s", name)
        if self._label_cache is not None:
            self._label_cache[name] = created["id"]
        return created["id"]

    def add_label(self, message_id: str, label_id: str) -> None:
        """Attach a label. Never removes labels and never deletes anything."""
        self._service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    # -- sending -----------------------------------------------------------

    def send_message(self, message: EmailMessage) -> str:
        """Send a prepared MIME message. Returns the Gmail message ID."""
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = (
            self._service.users()
            .messages()
            .send(userId="me", body={"raw": encoded})
            .execute()
        )
        return sent.get("id", "")


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _parse_message(raw: dict, *, max_body_chars: int) -> GmailMessage:
    payload = raw.get("payload", {})
    headers = {
        header.get("name", "").lower(): header.get("value", "")
        for header in payload.get("headers", [])
    }

    received_ms = raw.get("internalDate")
    received_at = (
        datetime.fromtimestamp(int(received_ms) / 1000, tz=timezone.utc)
        if received_ms
        else datetime.now(timezone.utc)
    )

    body = _extract_body(payload)
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n[truncated]"

    return GmailMessage(
        message_id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        subject=headers.get("subject", "(no subject)"),
        received_at=received_at,
        snippet=raw.get("snippet", ""),
        body=body,
        label_ids=list(raw.get("labelIds", [])),
    )


def _extract_body(payload: dict) -> str:
    """Pull readable text out of a MIME tree, preferring text/plain."""
    plain = _find_part(payload, "text/plain")
    if plain:
        return _normalise(plain)
    html = _find_part(payload, "text/html")
    if html:
        return _normalise(_html_to_text(html))
    return ""


def _find_part(payload: dict, mime_type: str) -> str:
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return _decode(data)
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found:
            return found
    return ""


def _decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    import html as html_module

    text = _SCRIPT_STYLE.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub(" ", text)
    return html_module.unescape(text)


def _normalise(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def is_not_found(exc: BaseException) -> bool:
    """True for a 404 - the message was deleted between listing and fetching."""
    return isinstance(exc, HttpError) and exc.resp.status == 404
