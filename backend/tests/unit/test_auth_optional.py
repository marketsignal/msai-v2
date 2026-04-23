"""Tests for :func:`msai.core.auth.get_current_user_or_none`.

The helper unlocks the signed-URL fallback path on ``GET /report`` — when
neither Bearer nor X-API-Key is present, the dependency returns ``None``
instead of raising 401, and the handler decides whether its ``?token=``
query parameter is sufficient. A regression that re-raises HTTPException
would short-circuit every iframe request.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from msai.core.auth import get_current_user_or_none
from msai.core.config import settings


class _FakeHeaders:
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    def get(self, key: str) -> str | None:
        return self._headers.get(key)


class _FakeRequest:
    """Minimal Request stand-in — we only read ``headers.get("X-API-Key")``."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = _FakeHeaders(headers or {})


@pytest.mark.asyncio
async def test_returns_none_when_no_credentials() -> None:
    """No Bearer, no X-API-Key — dependency unlocks fallback by returning None."""
    request = _FakeRequest()

    result = await get_current_user_or_none(
        request=request,  # type: ignore[arg-type]
        credentials=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_returns_claims_on_valid_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X-API-Key matching ``settings.msai_api_key`` returns the synthetic claims
    dict ``get_current_user`` emits for API-key auth — the signed-URL path is
    not reached.
    """
    monkeypatch.setattr(settings, "msai_api_key", "test-api-key")
    request = _FakeRequest(headers={"X-API-Key": "test-api-key"})

    result = await get_current_user_or_none(
        request=request,  # type: ignore[arg-type]
        credentials=None,
    )

    assert result is not None
    assert (
        result.get("sub") in {"cli", "test-api-key", "api-key"}
        or result.get("preferred_username") is not None
    )


@pytest.mark.asyncio
async def test_returns_none_on_invalid_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed Bearer token must NOT short-circuit with 401 — the helper
    swallows HTTPException so the caller's ``?token=`` fallback gets a shot.
    """

    async def _raises(*_a: object, **_kw: object) -> dict[str, object]:
        raise HTTPException(status_code=401, detail="bad token")

    monkeypatch.setattr("msai.core.auth.get_current_user", _raises)

    request = _FakeRequest(headers={"Authorization": "Bearer broken"})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="broken")

    result = await get_current_user_or_none(
        request=request,  # type: ignore[arg-type]
        credentials=credentials,
    )

    assert result is None


@pytest.mark.asyncio
async def test_api_key_with_wrong_value_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong X-API-Key — ``get_current_user`` raises 401; we translate to None."""
    monkeypatch.setattr(settings, "msai_api_key", "correct-key")
    request = _FakeRequest(headers={"X-API-Key": "wrong-key"})

    result = await get_current_user_or_none(
        request=request,  # type: ignore[arg-type]
        credentials=None,
    )

    assert result is None
