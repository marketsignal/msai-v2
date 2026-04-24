"""Typed exception hierarchy for Databento SDK failures.

Replaces generic RuntimeError string-matching with structured error
carriers so the bootstrap service can classify outcomes via isinstance()
+ http_status instead of brittle ``"401" in str(exc)`` patterns.
"""

from __future__ import annotations


class DatabentoError(Exception):
    """Base class — all Databento-surfaced failures carry http_status + dataset."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        dataset: str | None = None,
    ) -> None:
        self.http_status = http_status
        self.dataset = dataset
        super().__init__(message)


class DatabentoUnauthorizedError(DatabentoError):
    """401/403 — API key missing or dataset not entitled."""


class DatabentoRateLimitedError(DatabentoError):
    """429 — rate-limit exhausted after retries."""


class DatabentoUpstreamError(DatabentoError):
    """5xx or network failure after retries, or other 4xx that isn't 401/429."""
