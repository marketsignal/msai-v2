from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from msai.api import live as live_api
from msai.core.database import get_db
from msai.main import app
from msai.models import Base, Strategy
from msai.core.config import settings
import msai.services.live.portfolio_service as live_portfolio_service_module
from msai.services.graduation_service import GraduationService
from msai.services.risk_engine import RiskDecision


@pytest.fixture(autouse=True)
def _reset_app_state() -> None:
    app.dependency_overrides.clear()


def test_live_portfolio_crud_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_url: str,
) -> None:
    session_factory = _configure_live_portfolio_env(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        postgres_url=postgres_url,
    )

    async def _seed() -> None:
        async with session_factory() as session:
            session.add(
                Strategy(
                    id="strategy-1",
                    name="example.mean_reversion",
                    file_path="example/mean_reversion.py",
                    strategy_class="MeanReversionZScoreStrategy",
                    default_config={"lookback": 20},
                )
            )
            await session.commit()

    asyncio.run(_seed())

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        create_response = client.post(
            "/api/v1/live/portfolios",
            headers=headers,
            json={"name": "Live Core", "description": "Primary live basket"},
        )
        assert create_response.status_code == 200
        portfolio = create_response.json()
        assert portfolio["name"] == "Live Core"
        assert portfolio["active_revision"] is None

        add_response = client.post(
            f"/api/v1/live/portfolios/{portfolio['id']}/strategies",
            headers=headers,
            json={
                "strategy_id": "strategy-1",
                "config": {"lookback": 30, "trade_size": "1"},
                "instruments": ["SPY.XNAS"],
                "weight": 1.0,
            },
        )
        assert add_response.status_code == 200
        member = add_response.json()
        assert member["strategy_id"] == "strategy-1"
        assert member["order_index"] == 0

        snapshot_response = client.post(
            f"/api/v1/live/portfolios/{portfolio['id']}/snapshot",
            headers=headers,
        )
        assert snapshot_response.status_code == 200
        revision = snapshot_response.json()
        assert revision["is_frozen"] is True
        assert revision["revision_number"] == 1
        assert len(revision["strategies"]) == 1

        get_response = client.get(
            f"/api/v1/live/portfolios/{portfolio['id']}",
            headers=headers,
        )
        assert get_response.status_code == 200
        fetched = get_response.json()
        assert fetched["active_revision"]["id"] == revision["id"]
        assert fetched["draft_revision"] is None


def test_live_start_portfolio_uses_revision_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_url: str,
) -> None:
    session_factory = _configure_live_portfolio_env(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        postgres_url=postgres_url,
    )

    async def _seed() -> str:
        async with session_factory() as session:
            strategy = Strategy(
                id="strategy-1",
                name="example.mean_reversion",
                file_path="example/mean_reversion.py",
                strategy_class="MeanReversionZScoreStrategy",
                default_config={"lookback": 20},
            )
            session.add(strategy)
            await session.flush()

            from msai.services.live import PortfolioService, RevisionService

            portfolio_service = PortfolioService(session)
            revision_service = RevisionService(session)
            portfolio = await portfolio_service.create_portfolio(
                name="Live Start",
                description=None,
                created_by=None,
            )
            await portfolio_service.add_strategy(
                portfolio.id,
                strategy.id,
                {"lookback": 30, "trade_size": "2"},
                ["SPY.XNAS"],
                1,
            )
            revision = await revision_service.snapshot(portfolio.id)
            await session.commit()
            return revision.id

    revision_id = asyncio.run(_seed())

    async def _fake_canonicalize(*_: object, **__: object) -> list[str]:
        return ["SPY.XNAS"]

    started: dict[str, Any] = {}

    async def _fake_start(**kwargs: Any) -> str:
        started.update(kwargs)
        return "dep-portfolio-1"

    monkeypatch.setattr(live_api.instrument_service, "canonicalize_live_instruments", _fake_canonicalize)
    monkeypatch.setattr(live_api.live_runtime_client, "start", _fake_start)
    monkeypatch.setattr(
        live_api.risk_engine,
        "validate_start",
        lambda *args, **kwargs: asyncio.sleep(0, result=RiskDecision(True, "ok")),
    )
    strategy_file = tmp_path / "strategies" / "example" / "mean_reversion.py"

    class _FakeRegistry:
        def __init__(self, _root: Path) -> None:
            self._root = _root

        def resolve_path(self, _strategy: Strategy) -> Path:
            return strategy_file

    monkeypatch.setattr(live_api, "StrategyRegistry", _FakeRegistry)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/live/start-portfolio",
            headers={"X-API-Key": "msai-test-key"},
            json={
                "portfolio_revision_id": revision_id,
                "account_id": "DU123456",
                "paper_trading": True,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"deployment_id": "dep-portfolio-1"}
    assert started["portfolio_revision_id"] == revision_id
    assert started["account_id"] == "DU123456"
    assert started["paper_trading"] is True
    assert len(started["strategy_members"]) == 1
    assert started["strategy_members"][0]["strategy_id"] == "strategy-1"
    assert started["strategy_members"][0]["order_index"] == 0
    assert started["strategy_members"][0]["config"]["instrument_id"] == "SPY.XNAS"
    assert isinstance(started["identity_signature"], str) and started["identity_signature"]


def _configure_live_portfolio_env(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_url: str,
) -> async_sessionmaker[AsyncSession]:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(settings, "database_url", postgres_url)
    strategy_file = tmp_path / "strategies" / "example" / "mean_reversion.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text(
        "\n".join(
            [
                "from nautilus_trader.trading.config import StrategyConfig",
                "from nautilus_trader.trading.strategy import Strategy",
                "",
                "class MeanReversionZScoreConfig(StrategyConfig, frozen=True):",
                "    instrument_id: str",
                "    bar_type: str",
                "    lookback: int = 20",
                "    trade_size: str = '1'",
                "",
                "class MeanReversionZScoreStrategy(Strategy):",
                "    def __init__(self, config: MeanReversionZScoreConfig) -> None:",
                "        super().__init__(config=config)",
            ]
        )
    )

    graduation_root = tmp_path / "graduation"
    graduation_root.mkdir(parents=True, exist_ok=True)
    (graduation_root / "candidate-1.json").write_text(
        json.dumps(
            {
                "id": "candidate-1",
                "strategy_id": "strategy-1",
                "stage": "live_candidate",
                "created_at": "2026-04-16T00:00:00+00:00",
                "updated_at": "2026-04-16T00:00:00+00:00",
            }
        )
    )
    graduation_service = GraduationService(root=graduation_root)
    monkeypatch.setattr(
        live_portfolio_service_module,
        "GraduationService",
        lambda root=None: graduation_service,
    )

    engine = create_async_engine(postgres_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_schema())

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    return session_factory
