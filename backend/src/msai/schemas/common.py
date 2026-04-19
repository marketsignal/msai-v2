"""Common response schemas shared across API endpoints.

These Pydantic models define the standard response shapes for success
messages, error payloads, and other reusable response structures.
"""

from __future__ import annotations

from pydantic import BaseModel


class MessageResponse(BaseModel):
    """Generic success response containing a human-readable message."""

    message: str


class ErrorResponse(BaseModel):
    """Standard error response following the API design guidelines.

    Attributes:
        error: Machine-readable error code (e.g. ``"VALIDATION_ERROR"``).
        detail: Optional human-readable description of what went wrong.
        request_id: Correlation identifier from the ``X-Request-ID`` header,
            useful for log tracing.
    """

    error: str
    detail: str | None = None
    request_id: str | None = None
