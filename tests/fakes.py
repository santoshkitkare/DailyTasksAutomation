"""In-memory stand-ins for every external service.

No test in this suite is allowed to touch the network, so these implement just
enough of each client's surface for the workers to run against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from daily_ai_automation.integrations.image_generation import (
    GeneratedImage,
    ImageGenerationError,
)
from daily_ai_automation.integrations.llm import TokenUsage


class FakeGmailClient:
    """Records what would have been written instead of writing it."""

    def __init__(self, messages=None, *, address="me@example.com"):
        self._messages = {m.message_id: m for m in (messages or [])}
        self._address = address
        self.labels = {"INBOX": "Label_0"}
        self.labeled: list[tuple[str, str]] = []
        self.sent: list = []
        self.send_error: Exception | None = None
        self.label_error: Exception | None = None

    # -- profile / reading -------------------------------------------------

    @property
    def address(self) -> str:
        return self._address

    def get_profile(self) -> dict:
        return {"emailAddress": self._address}

    def search_message_ids(self, query, *, max_results):
        self.last_query = query
        return list(self._messages)[:max_results]

    def get_message(self, message_id, *, max_body_chars):
        return self._messages[message_id]

    # -- writing -----------------------------------------------------------

    def label_id(self, name):
        return self.labels.get(name)

    def ensure_label(self, name):
        if self.label_error:
            raise self.label_error
        return self.labels.setdefault(name, f"Label_{len(self.labels)}")

    def add_label(self, message_id, label_id):
        if self.label_error:
            raise self.label_error
        self.labeled.append((message_id, label_id))

    def send_message(self, message):
        if self.send_error:
            raise self.send_error
        self.sent.append(message)
        return f"sent-{len(self.sent)}"


@dataclass
class FakeLLMClient:
    """Returns canned structured responses; records what it was asked."""

    responses: list = field(default_factory=list)
    error: Exception | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    calls: list = field(default_factory=list)

    def parse(self, *, model, system, user_content, schema, max_tokens=8000):
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user_content": user_content,
                "schema": schema,
            }
        )
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError(f"FakeLLMClient has no response left for {schema}")
        self.usage.calls += 1
        return self.responses.pop(0)


class FakeDriveClient:
    def __init__(self, payload: bytes = b"", error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.requested: list[str] = []

    def download_xlsx(self, file_id):
        self.requested.append(file_id)
        if self.error:
            raise self.error
        return self.payload

    def describe(self, file_id):
        if self.error:
            raise self.error
        return None


class FakeImageClient:
    def __init__(self, *, error: Exception | None = None, data: bytes = b"PNGDATA"):
        self.error = error
        self.data = data
        self.prompts: list[str] = []

    def generate_and_save(self, prompt, destination):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        destination.parent.mkdir(parents=True, exist_ok=True)
        path = destination.with_suffix(".png")
        path.write_bytes(self.data)
        return GeneratedImage(data=self.data, mime_type="image/png", path=path)

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return GeneratedImage(data=self.data, mime_type="image/png")


__all__ = [
    "FakeDriveClient",
    "FakeGmailClient",
    "FakeImageClient",
    "FakeLLMClient",
    "ImageGenerationError",
]
