"""Anthropic Claude access, constrained to structured output.

Every call here goes through ``messages.parse`` with a Pydantic schema. That is
deliberate and is the technical basis for PRD section 35: the model's entire
output vocabulary is a fixed set of enums and strings, so a malicious email
cannot cause it to emit an instruction to the application. It can, at worst,
produce a wrong classification - which the policy engine then vetoes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from ..supervisor.retry import TerminalError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Wrapper placed around every piece of untrusted content. The model is told,
#: in the system prompt, that anything between these markers is data.
UNTRUSTED_OPEN = "<untrusted_email_content>"
UNTRUSTED_CLOSE = "</untrusted_email_content>"


@dataclass
class TokenUsage:
    """Running token/latency totals for the run report (PRD section 37)."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    latency_seconds: float = 0.0

    def add(self, usage, elapsed: float) -> None:  # noqa: ANN001 - SDK usage obj
        self.calls += 1
        self.latency_seconds += elapsed
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_seconds": round(self.latency_seconds, 2),
        }


class LLMClient:
    """Thin wrapper over the Anthropic SDK.

    The SDK already retries connection errors, 429s and 5xx internally, so this
    class does not add its own retry loop; it only translates the errors that
    are *not* worth retrying into TerminalError so the supervisor stops early
    instead of waiting out three backoffs on a bad API key.
    """

    def __init__(self, api_key: str, *, max_retries: int = 2) -> None:
        if not api_key:
            raise TerminalError(
                "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example)."
            )
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self.usage = TokenUsage()

    def parse(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        schema: type[T],
        max_tokens: int = 8000,
    ) -> T:
        """One structured request. Returns a validated instance of ``schema``."""
        started = time.monotonic()
        try:
            response = self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                output_format=schema,
            )
        except anthropic.NotFoundError as exc:
            raise TerminalError(
                f"Model {model!r} was not found. Check ai.* in config.yaml."
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise TerminalError(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise TerminalError(f"Anthropic denied the request: {exc}") from exc
        except anthropic.BadRequestError as exc:
            # A malformed request will fail identically every time.
            raise TerminalError(f"Anthropic rejected the request: {exc}") from exc

        elapsed = time.monotonic() - started
        self.usage.add(response.usage, elapsed)

        parsed = response.parsed_output
        if parsed is None:
            raise ValueError(
                f"Model returned no parsable {schema.__name__}; "
                f"stop_reason={response.stop_reason}"
            )
        return parsed


def wrap_untrusted(text: str) -> str:
    """Fence untrusted content and neutralise attempts to close the fence."""
    cleaned = text.replace(UNTRUSTED_CLOSE, "[removed]")
    return f"{UNTRUSTED_OPEN}\n{cleaned}\n{UNTRUSTED_CLOSE}"


@dataclass
class PromptLibrary:
    """Loads the versioned prompt files from daily_ai_automation/prompts."""

    directory: Path
    _cache: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str:
        if name not in self._cache:
            path = self.directory / f"{name}.md"
            if not path.exists():
                raise FileNotFoundError(f"Prompt file not found: {path}")
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]
