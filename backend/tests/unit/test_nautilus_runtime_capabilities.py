"""Owning checks for the installed-engine research/live boundary."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.services.nautilus import runtime_capabilities as capabilities


@pytest.fixture
def research_runtime(monkeypatch):
    monkeypatch.setattr(capabilities, "engine_version", lambda: "2.0.0rc6")
    monkeypatch.setenv("MSAI_ALLOW_LIVE", "true")


def test_installed_engine_is_authority(research_runtime):
    assert capabilities.is_v2_research_runtime()
    with pytest.raises(capabilities.UnsupportedRuntimeError, match="research-only"):
        capabilities.require_live_runtime("live start")


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/v1/live/start-portfolio", {"portfolio_revision_id": str(uuid4())}),
        ("post", "/api/v1/live/resume", None),
        ("post", "/api/v1/live/resume/U1234567", None),
        ("get", "/api/v1/live/positions", None),
        ("get", "/api/v1/live/release-readiness", None),
    ],
)
def test_auth_before_unsupported_refusal(research_runtime, method, path, body):
    app.dependency_overrides.clear()
    try:
        response = (
            getattr(TestClient(app), method)(path, json=body)
            if method == "post"
            else TestClient(app).get(path)
        )
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/v1/live/start-portfolio", {"portfolio_revision_id": str(uuid4())}),
        ("post", "/api/v1/live/resume", None),
        ("post", "/api/v1/live/resume/U1234567", None),
        ("get", "/api/v1/live/positions", None),
        ("get", "/api/v1/live/release-readiness", None),
    ],
)
def test_refusal_precedes_db_and_commands(research_runtime, method, path, body):
    async def forbidden_db():
        raise AssertionError("unsupported operation reached DB")
        yield

    app.dependency_overrides[get_current_user] = lambda: {"sub": "unit-user"}
    app.dependency_overrides[get_db] = forbidden_db
    try:
        client = TestClient(app)
        response = (
            getattr(client, method)(path, json=body) if method == "post" else client.get(path)
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "unsupported_nautilus_runtime"
    finally:
        app.dependency_overrides.clear()


async def test_active_live_state_prevents_startup(research_runtime, monkeypatch):
    import msai.main as main

    monkeypatch.setattr(main, "_has_incompatible_live_state", AsyncMock(return_value=True))
    with pytest.raises(capabilities.UnsupportedRuntimeError, match="active live state"):
        async with main.lifespan(app):
            pytest.fail("incompatible startup yielded")


@pytest.mark.parametrize("counts,expected", [([1], True), ([0, 1], True), ([0, 0], False)])
async def test_startup_checks_deployments_and_orphan_processes(monkeypatch, counts, expected):
    import msai.core.database as database
    import msai.main as main

    session = AsyncMock()
    session.execute.side_effect = [MagicMock(scalar_one=lambda n=n: n) for n in counts]
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(database, "async_session_factory", lambda: context)
    assert await main._has_incompatible_live_state() is expected
    assert session.execute.await_count == len(counts)


def test_native_independent_date_helper(research_runtime):
    from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today

    assert exchange_local_today().year >= 2026


async def test_supervisor_refuses_before_connections(research_runtime):
    from msai.live_supervisor.__main__ import _async_main

    assert await _async_main() == 2


async def test_research_lifespan_does_not_start_broker_or_projection(research_runtime, monkeypatch):
    import msai.api.account as account
    import msai.main as main
    import msai.services.live.broker_credentials_store as stores

    projection = AsyncMock()
    broker = AsyncMock()
    store = MagicMock()
    factory = MagicMock(return_value=store)
    monkeypatch.setattr(stores, "get_broker_credentials_store", factory)
    monkeypatch.setattr(main, "_has_incompatible_live_state", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_ensure_api_key_user", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_start_projection_tasks", projection)
    monkeypatch.setattr(account, "start_ib_probe_task", broker)
    async with main.lifespan(app):
        assert app.state.broker_credentials_store is store
        assert app.state.gateway_router is not None
    factory.assert_called_once()
    store.ping.assert_not_called()
    store.get.assert_not_called()
    store.put.assert_not_called()
    projection.assert_not_called()
    broker.assert_not_called()


async def test_databento_definition_refuses_before_sdk_or_file_io(research_runtime, tmp_path):
    from msai.services.data_sources.databento_client import DatabentoClient

    target = tmp_path / "must-not-create" / "definition.dbn.zst"
    with pytest.raises(capabilities.UnsupportedRuntimeError, match="definition"):
        await DatabentoClient(api_key="dummy-not-a-vendor-key").fetch_definition_instruments(
            "AAPL", "2025-01-02", "2025-01-03", dataset="EQUS.MINI", target_path=target
        )
    assert not target.parent.exists()


def test_cold_reader_refuses_before_native_cache_import(research_runtime):
    from msai.api.live_deps import get_position_reader

    with pytest.raises(capabilities.UnsupportedRuntimeError, match="cold-cache"):
        get_position_reader()


def test_websocket_auth_then_refusal_before_deployment_or_redis(research_runtime, monkeypatch):
    import msai.api.websocket as websocket
    from msai.core.config import settings

    monkeypatch.setattr(settings, "msai_api_key", "unit-runtime-key")
    load = AsyncMock(side_effect=AssertionError("unsupported WS touched live state"))
    monkeypatch.setattr(websocket, "_load_deployment", load)
    with TestClient(app).websocket_connect(f"/api/v1/live/stream/{uuid4()}") as ws:
        ws.send_text("unit-runtime-key")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4503
    load.assert_not_called()


async def test_stop_stays_usable_on_research_runtime(research_runtime):
    from msai.api.live import live_stop
    from msai.schemas.live import LiveStopRequest

    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none = MagicMock(return_value=None)
    # A nonexistent deployment must still yield the ordinary 404, rather than
    # disabling exit controls with the unsupported-opening-operation guard.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await live_stop(
            LiveStopRequest(deployment_id=uuid4()),
            claims={"sub": "unit-user"},
            db=db,
            bus=MagicMock(),
        )
    assert exc.value.status_code == 404
    db.execute.assert_awaited_once()
