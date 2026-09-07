"""Retry classification and backoff (PRD section 31).

The distinction that matters is not "did it fail" but "would trying again
plausibly succeed". Retrying a bad OAuth token or an invalid recipient address
just burns three attempts and delays the report.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class TerminalError(Exception):
    """A failure that will recur identically on retry. Do not retry."""


class TransientError(Exception):
    """A failure that may resolve on its own. Safe to retry."""


# Exception *type names* treated as transient. Matched by name so this module
# does not have to import googleapiclient or anthropic.
TRANSIENT_TYPE_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectionError",
        "InternalServerError",
        "OperationalError",
        "RateLimitError",
        "ServerTimeoutError",
        "TimeoutError",
        "TransientError",
    }
)

# HTTP statuses worth another attempt. 403 is deliberately absent: on Google
# APIs it is usually a missing scope or a disabled API, not a rate limit.
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Decide whether an exception is worth retrying."""
    if isinstance(exc, TerminalError):
        return False
    if isinstance(exc, TransientError):
        return True
    if type(exc).__name__ in TRANSIENT_TYPE_NAMES:
        return True

    # googleapiclient.errors.HttpError and anthropic.APIStatusError both expose
    # a status code, just under different attribute names.
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status in TRANSIENT_STATUS_CODES

    return False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: tuple[int, ...] = (30, 120, 300)

    def delay_for(self, attempt: int) -> int:
        """Seconds to wait before attempt number `attempt` (1-based).

        Attempt 1 never waits. Beyond the configured list, the last value
        repeats rather than growing without bound.
        """
        if attempt <= 1 or not self.backoff_seconds:
            return 0
        index = min(attempt - 2, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index]


@dataclass
class RetryOutcome:
    """What happened across all attempts, for the execution record."""

    attempts: int = 0
    last_error: BaseException | None = None


def run_with_retry(
    func: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, BaseException, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[T, RetryOutcome]:
    """Call `func`, retrying only transient failures.

    `sleep` is injectable so tests exercise the backoff schedule without
    actually waiting five minutes.
    """
    outcome = RetryOutcome()
    for attempt in range(1, policy.max_attempts + 1):
        outcome.attempts = attempt
        try:
            return func(), outcome
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome.last_error = exc
            if not is_transient(exc) or attempt == policy.max_attempts:
                raise
            delay = policy.delay_for(attempt + 1)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if delay:
                sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
