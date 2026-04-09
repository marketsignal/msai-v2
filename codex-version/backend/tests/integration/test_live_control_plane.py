from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from msai.api import live as live_api
from msai.core import queue as queue_module
from msai.core.config import settings
from msai.core.database import get_db
from msai.main import app
from msai.models import Strategy
from msai.services.live_updates import publish_live_snapshot_sync
from msai.services.nautilus.trading_node import DeploymentStopResult


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _RowResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return self._rows


@pytest.fixture(autouse=True)
def _reset_app_state() -> AsyncGenerator[None, None]:
    app.dependency_overrides.clear()
    _discard_redis_pool()
    yield
    app.dependency_overrides.clear()
    _discard_redis_pool()


def _discard_redis_pool() -> None:
    pool = queue_module._pool
    queue_module._pool = None
    if pool is None:
        return
    try:
        asyncio.run(pool.aclose())
    except RuntimeError:
        return


def test_live_start_stop_round_trip_uses_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    strategy = Strategy(
        id="strategy-ema",
        name="example.ema_cross",
        file_path="example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        default_config={"fast_ema_period": 10, "slow_ema_period": 30, "trade_size": "1"},
    )

    class _FakeSession:
        async def get(self, model: type[object], key: str) -> Strategy | None:
            if model is Strategy and key == strategy.id:
                return strategy
            return None

        async def commit(self) -> None:
            return None

    async def _override_db() -> AsyncGenerator[_FakeSession, None]:
        yield _FakeSession()

    app.dependency_overrides[get_db] = _override_db

    async def _fake_user_id(*_: object, **__: object) -> str:
        return "user-1"

    async def _fake_canonicalize(*_: object, **__: object) -> list[str]:
        return ["AAPL.XNAS", "MSFT.XNAS"]

    started: dict[str, Any] = {}

    async def _fake_start(**kwargs: Any) -> str:
        started.update(kwargs)
        return "dep-123"

    async def _fake_liquidate_and_stop(
        deployment_id: str,
        *,
        reason: str,
    ) -> DeploymentStopResult:
        assert deployment_id == "dep-123"
        assert "graceful stop" in reason
        return DeploymentStopResult(found=True, stopped=True)

    monkeypatch.setattr(live_api, "resolve_user_id_from_claims", _fake_user_id)
    monkeypatch.setattr(live_api.instrument_service, "canonicalize_live_instruments", _fake_canonicalize)
    monkeypatch.setattr(live_api.live_runtime_client, "start", _fake_start)
    monkeypatch.setattr(live_api.live_runtime_client, "stop", _fake_liquidate_and_stop)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/live/start",
            headers={"X-API-Key": "msai-test-key"},
            json={
                "strategy_id": "strategy-ema",
                "config": {"trade_size": "1"},
                "instruments": ["AAPL.XNAS", "MSFT.XNAS"],
                "paper_trading": True,
            },
        )

        assert response.status_code == 200
        assert response.json() == {"deployment_id": "dep-123"}
        assert started["strategy_id"] == "strategy-ema"
        assert started["started_by"] == "user-1"
        assert started["instruments"] == ["AAPL.XNAS", "MSFT.XNAS"]
        assert started["paper_trading"] is True
        assert started["config"]["instrument_id"] == "AAPL.XNAS"
        assert started["config"]["bar_type"] == "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"

        stop_response = client.post(
            "/api/v1/live/stop",
            headers={"X-API-Key": "msai-test-key"},
            json={"deployment_id": "dep-123"},
        )

        assert stop_response.status_code == 200
        assert stop_response.json() == {"status": "stopped"}


def test_live_status_and_positions_use_runtime_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    status_rows = [
        {
            "id": "dep-1",
            "strategy_id": "strategy-ema",
            "status": "running",
            "started_at": "2026-04-07T12:00:00Z",
            "process_alive": True,
            "control_mode": "attached",
            "runtime_fresh": True,
            "paper_trading": True,
            "broker_connected": True,
            "broker_mock_mode": False,
            "broker_updated_at": "2026-04-07T12:00:00Z",
            "broker_open_positions": 0,
            "broker_open_orders": 0,
            "broker_exposure_detected": False,
        }
    ]

    class _FakeSession:
        async def execute(self, *_: object, **__: object) -> _RowResult:
            return _RowResult([("strategy-ema", "example.ema_cross")])

    async def _override_db() -> AsyncGenerator[_FakeSession, None]:
        yield _FakeSession()

    async def _fake_status() -> list[dict[str, Any]]:
        return status_rows

    async def _fake_snapshots(name: str) -> list[dict[str, Any]]:
        if name == "status":
            return [
                {
                    "scope": "dep-1",
                    "generated_at": "2026-04-07T12:05:00Z",
                    "data": {
                        "status": "liquidating",
                        "daily_pnl": 12.5,
                        "open_positions": 1,
                        "open_orders": 0,
                        "updated_at": "2026-04-07T12:05:00Z",
                    },
                }
            ]
        if name == "positions":
            return [
                {
                    "scope": "dep-1",
                    "generated_at": "2026-04-07T12:05:00Z",
                    "data": [
                        {
                            "deployment_id": "dep-1",
                            "instrument": "AAPL.XNAS",
                            "quantity": 10,
                            "avg_price": 210.5,
                            "current_price": 212.1,
                            "unrealized_pnl": 16.0,
                            "market_value": 2121.0,
                            "paper_trading": True,
                        }
                    ],
                }
            ]
        return []

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(live_api.live_runtime_client, "status", _fake_status)
    monkeypatch.setattr(live_api, "load_live_snapshots", _fake_snapshots)

    with TestClient(app) as client:
        status_response = client.get("/api/v1/live/status", headers={"X-API-Key": "msai-test-key"})
        positions_response = client.get("/api/v1/live/positions", headers={"X-API-Key": "msai-test-key"})

        assert status_response.status_code == 200
        assert status_response.json() == [
            {
                "id": "dep-1",
                "strategy": "example.ema_cross",
                "status": "liquidating",
                "started_at": "2026-04-07T12:00:00Z",
                "daily_pnl": 12.5,
                "process_alive": True,
                "control_mode": "attached",
                "runtime_fresh": True,
                "paper_trading": True,
                "open_positions": 1,
                "open_orders": 0,
                "updated_at": "2026-04-07T12:05:00Z",
                "reason": None,
                "broker_connected": True,
                "broker_mock_mode": False,
                "broker_updated_at": "2026-04-07T12:00:00Z",
                "broker_open_positions": 0,
                "broker_open_orders": 0,
                "broker_exposure_detected": False,
            }
        ]
        assert positions_response.status_code == 200
        assert positions_response.json() == [
            {
                "deployment_id": "dep-1",
                "instrument": "AAPL.XNAS",
                "quantity": 10,
                "avg_price": 210.5,
                "current_price": 212.1,
                "unrealized_pnl": 16.0,
                "market_value": 2121.0,
                "paper_trading": True,
            }
        ]


def test_live_stream_replays_snapshot_and_streams_updates(
    monkeypatch: pytest.MonkeyPatch,
    redis_url: str,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(settings, "redis_url", redis_url)

    publish_live_snapshot_sync(
        "status",
        {
            "status": "running",
            "daily_pnl": 7.5,
            "open_positions": 1,
            "open_orders": 0,
        },
        scope="dep-stream",
    )

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/live/stream") as websocket:
            websocket.send_text("msai-test-key")

            snapshot = websocket.receive_json()
            assert snapshot["type"] == "status.snapshot"
            assert snapshot["scope"] == "dep-stream"
            assert snapshot["data"]["status"] == "running"

            publish_live_snapshot_sync(
                "orders",
                [
                    {
                        "deployment_id": "dep-stream",
                        "instrument": "AAPL.XNAS",
                        "status": "submitted",
                        "paper_trading": True,
                    }
                ],
                scope="dep-stream",
            )

            update = websocket.receive_json()
            assert update["type"] == "orders.snapshot"
            assert update["scope"] == "dep-stream"
            assert update["data"][0]["instrument"] == "AAPL.XNAS"
