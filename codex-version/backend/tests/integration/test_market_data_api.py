from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from msai.api import market_data as market_data_api
from msai.core.config import settings
from msai.main import app
from msai.services.daily_universe import DailyUniverseService


@pytest.fixture(autouse=True)
def _reset_market_data_app_state() -> None:
    app.dependency_overrides.clear()


def test_market_data_ingest_queue_endpoint_uses_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    captured: dict[str, object] = {}

    async def _fake_enqueue(
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str,
        dataset: str | None,
        schema: str,
    ) -> None:
        captured.update(
            {
                "asset_class": asset_class,
                "symbols": symbols,
                "start": start,
                "end": end,
                "provider": provider,
                "dataset": dataset,
                "schema": schema,
            }
        )

    monkeypatch.setattr(market_data_api, "_enqueue_ingestion_request", _fake_enqueue)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/market-data/ingest",
            headers={"X-API-Key": "msai-test-key"},
            json={
                "asset_class": "equities",
                "symbols": ["SPY", "IWM"],
                "start": "2026-04-01",
                "end": "2026-04-02",
                "provider": "databento",
                "dataset": "ARCX.PILLAR",
                "schema": "ohlcv-1m",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert captured == {
        "asset_class": "equities",
        "symbols": ["SPY", "IWM"],
        "start": "2026-04-01",
        "end": "2026-04-02",
        "provider": "databento",
        "dataset": "ARCX.PILLAR",
        "schema": "ohlcv-1m",
    }


def test_market_data_daily_ingest_endpoint_queues_yesterday_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    captured: dict[str, object] = {}

    class _FakeDate(date):
        @classmethod
        def today(cls) -> _FakeDate:
            return cls(2026, 4, 7)

    async def _fake_enqueue(
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str,
        dataset: str | None,
        schema: str,
    ) -> None:
        captured.update(
            {
                "asset_class": asset_class,
                "symbols": symbols,
                "start": start,
                "end": end,
                "provider": provider,
                "dataset": dataset,
                "schema": schema,
            }
        )

    monkeypatch.setattr(market_data_api, "date", _FakeDate)
    monkeypatch.setattr(market_data_api, "_enqueue_ingestion_request", _fake_enqueue)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/market-data/ingest-daily",
            headers={"X-API-Key": "msai-test-key"},
            json={
                "asset_class": "futures",
                "symbols": ["ES.v.0", "NQ.v.0"],
                "provider": "databento",
                "dataset": "GLBX.MDP3",
                "schema": "ohlcv-1m",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "queued",
        "start": "2026-04-06",
        "end": "2026-04-07",
    }
    assert captured == {
        "asset_class": "futures",
        "symbols": ["ES.v.0", "NQ.v.0"],
        "start": "2026-04-06",
        "end": "2026-04-07",
        "provider": "databento",
        "dataset": "GLBX.MDP3",
        "schema": "ohlcv-1m",
    }


def test_market_data_daily_universe_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    service = DailyUniverseService(path=tmp_path / "daily-universe.json")
    monkeypatch.setattr(market_data_api, "daily_universe_service", service)

    with TestClient(app) as client:
      headers = {"X-API-Key": "msai-test-key"}

      get_response = client.get("/api/v1/market-data/daily-universe", headers=headers)
      assert get_response.status_code == 200
      assert len(get_response.json()["requests"]) >= 1

      put_response = client.put(
          "/api/v1/market-data/daily-universe",
          headers=headers,
          json={
              "requests": [
                  {
                      "asset_class": "equities",
                      "symbols": ["SPY", "QQQ"],
                      "provider": "databento",
                      "dataset": "EQUS.MINI",
                      "schema": "ohlcv-1m",
                  }
              ]
          },
      )
      assert put_response.status_code == 200
      assert put_response.json()["requests"][0]["symbols"] == ["SPY", "QQQ"]
      persisted = json.loads((tmp_path / "daily-universe.json").read_text())
      assert persisted["requests"][0]["dataset"] == "EQUS.MINI"


def test_market_data_configured_daily_ingest_endpoint_queues_saved_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    service = DailyUniverseService(path=tmp_path / "daily-universe.json")
    service.save_requests(
        [
            market_data_api.DailyIngestRequest(
                asset_class="equities",
                symbols=["SPY"],
                provider="databento",
                dataset="EQUS.MINI",
                schema="ohlcv-1m",
            ),
            market_data_api.DailyIngestRequest(
                asset_class="futures",
                symbols=["ES.v.0"],
                provider="databento",
                dataset="GLBX.MDP3",
                schema="ohlcv-1m",
            ),
        ]
    )
    monkeypatch.setattr(market_data_api, "daily_universe_service", service)

    captured: list[dict[str, object]] = []

    async def _fake_enqueue(
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str,
        dataset: str | None,
        schema: str,
    ) -> None:
        captured.append(
            {
                "asset_class": asset_class,
                "symbols": symbols,
                "start": start,
                "end": end,
                "provider": provider,
                "dataset": dataset,
                "schema": schema,
            }
        )

    class _FakeDate(date):
        @classmethod
        def today(cls) -> _FakeDate:
            return cls(2026, 4, 7)

    monkeypatch.setattr(market_data_api, "date", _FakeDate)
    monkeypatch.setattr(market_data_api, "_enqueue_ingestion_request", _fake_enqueue)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/market-data/ingest-daily-configured",
            headers={"X-API-Key": "msai-test-key"},
        )

    assert response.status_code == 200
    assert response.json()["request_count"] == 2
    assert captured == [
        {
            "asset_class": "equities",
            "symbols": ["SPY"],
            "start": "2026-04-06",
            "end": "2026-04-07",
            "provider": "databento",
            "dataset": "EQUS.MINI",
            "schema": "ohlcv-1m",
        },
        {
            "asset_class": "futures",
            "symbols": ["ES.v.0"],
            "start": "2026-04-06",
            "end": "2026-04-07",
            "provider": "databento",
            "dataset": "GLBX.MDP3",
            "schema": "ohlcv-1m",
        },
    ]
