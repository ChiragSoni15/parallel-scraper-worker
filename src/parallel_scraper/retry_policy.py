"""Transient/permanent error classification and tenacity decorator factory."""
from __future__ import annotations

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)


class TransientError(Exception):
    """Retryable failure (timeout, 5xx, rate-limit, network glitch)."""


class PermanentError(Exception):
    """Non-retryable failure (invalid placeId, 404, removed listing)."""


_TRANSIENT_HINTS = (
    "timeout", "navigation", "interrupted", "rate limited",
    "429", "503", "504", "502", "connection", "network",
)
_PERMANENT_HINTS = (
    "not found", "no longer exists", "invalid place_id", "404",
    "permanently closed",
)


def classify(error_text: str) -> Exception:
    """Map an error string to a TransientError or PermanentError instance.

    Defaults to TransientError so tenacity retries — it gives up after N anyway.
    """
    t = (error_text or "").lower()
    if any(h in t for h in _PERMANENT_HINTS):
        return PermanentError(error_text)
    if any(h in t for h in _TRANSIENT_HINTS):
        return TransientError(error_text)
    return TransientError(error_text)


def transient_retry(max_attempts: int):
    """Build a tenacity decorator that retries TransientError up to max_attempts times."""
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=2.0, max=30.0),
        retry=retry_if_exception_type(TransientError),
        reraise=True,
    )
