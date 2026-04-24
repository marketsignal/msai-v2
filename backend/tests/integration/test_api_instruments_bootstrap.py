"""Integration tests for POST /api/v1/instruments/bootstrap.

Uses a module-local ``client`` fixture that overrides ``get_session_factory``
(pointing at testcontainers session_factory) AND monkey-patches the
DatabentoClient import site in api/instruments.py to inject the mock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from msai.core.database import get_session_factory
from msai.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    mock_databento,  # from conftest_databento.py
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with get_session_factory + DatabentoClient overridden."""
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    # Patch DatabentoClient at import site inside api/instruments.py so
    # the endpoint constructs our mock instead of a real client.
    import msai.api.instruments as instruments_module

    original_cls = instruments_module.DatabentoClient
    instruments_module.DatabentoClient = lambda *a, **kw: mock_databento  # type: ignore[misc]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
    finally:
        instruments_module.DatabentoClient = original_cls  # type: ignore[misc]
        app.dependency_overrides.pop(get_session_factory, None)


@pytest.mark.asyncio
async def test_all_success_returns_200(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["AAPL"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["created"] == 1
    assert body["results"][0]["registered"] is True


@pytest.mark.asyncio
async def test_mixed_returns_207(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["AAPL", "BRK.B"]},
    )
    assert resp.status_code == 207
    body = resp.json()
    outcomes = {r["symbol"]: r["outcome"] for r in body["results"]}
    assert outcomes["AAPL"] == "created"
    assert outcomes["BRK.B"] == "ambiguous"


@pytest.mark.asyncio
async def test_all_failed_returns_422(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": ["BRK.B"]},  # single ambiguous
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["results"][0]["outcome"] == "ambiguous"


@pytest.mark.asyncio
async def test_unsupported_provider_returns_422_pydantic_envelope(
    client: httpx.AsyncClient,
) -> None:
    """provider: Literal['databento'] rejects 'polygon' at Pydantic parse time.

    Returns Pydantic's default 422 envelope (intentional boundary — business-logic
    failures below use the project {error:...} envelope)."""
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "polygon", "symbols": ["AAPL"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_symbols_returns_422(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/instruments/bootstrap",
        json={"provider": "databento", "symbols": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_databento_not_configured_returns_500(
    session_factory: async_sessionmaker[AsyncSession],
    mock_databento,
) -> None:
    """If the DatabentoClient has no api_key, the endpoint returns 500
    with code=DATABENTO_NOT_CONFIGURED. Guards the config-check branch
    in api/instruments.py against silent-success regressions."""
    mock_databento.api_key = ""  # trip the not-configured branch

    app.dependency_overrides[get_session_factory] = lambda: session_factory
    import msai.api.instruments as instruments_module

    original_cls = instruments_module.DatabentoClient
    instruments_module.DatabentoClient = lambda *a, **kw: mock_databento  # type: ignore[misc]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.post(
                "/api/v1/instruments/bootstrap",
                json={"provider": "databento", "symbols": ["AAPL"]},
            )
    finally:
        instruments_module.DatabentoClient = original_cls  # type: ignore[misc]
        app.dependency_overrides.pop(get_session_factory, None)

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "DATABENTO_NOT_CONFIGURED"
