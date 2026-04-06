"""Shared test fixtures for the MSAI v2 test suite."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import httpx
import pytest

from msai.core.auth import get_current_user
from msai.main import app

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


@pytest.fixture(autouse=True)
def _override_auth() -> Generator[None, None, None]:
    """Override get_current_user for all tests so auth-protected endpoints pass.

    Individual test modules can add further overrides (e.g. mock DB) on top
    of this one.  The autouse cleanup restores the overrides dict after each test.
    """
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")
