"""Cross-router shared helpers. Kept intentionally small — anything
router-specific belongs in that router's module."""

from __future__ import annotations

from fastapi.responses import JSONResponse

__all__ = ["error_response"]


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build the canonical ``{"error": {"code", "message"}}`` envelope.

    Every error path in every router uses ``JSONResponse`` (not
    ``HTTPException``) because FastAPI wraps ``HTTPException.detail`` under
    ``{"detail": ...}`` while ``.claude/rules/api-design.md`` requires the
    envelope at top-level.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
