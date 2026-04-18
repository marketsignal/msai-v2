from __future__ import annotations

import queue
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from msai.core.config import settings
from msai.models import (
    Base,
    LiveDeployment,
    LiveDeploymentStrategy,
    LivePortfolio,
    LivePortfolioRevision,
    LivePortfolioRevisionStrategy,
    Strategy,
    User,
)
from msai.services.nautilus import trading_node as trading_node_module
from msai.services.nautilus.trading_node import TradingNodeManager


@pytest.mark.asyncio
async def test_trading_node_manager_start_portfolio_persists_runtime_members(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_url", postgres_url)

    engine = create_async_engine(postgres_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(trading_node_module, "async_session_factory", session_factory)
    monkeypatch.setattr(trading_node_module, "_NAUTILUS_IMPORT_ERROR", None)
    monkeypatch.setattr(trading_node_module, "_publish_live_update_safely", _async_noop)
    monkeypatch.setattr(trading_node_module, "_load_status_snapshots_by_scope", _async_empty_dict)

    async def _no_overlap(self, *, instruments: list[str], paper_trading: bool) -> None:
        assert instruments == ["SPY.XNAS", "QQQ.XNAS"]
        assert paper_trading is True

    async def _fake_startup_status(
        self,
        *,
        deployment_id: str,
        process: Any,
        startup_queue: Any,
    ) -> dict[str, Any]:
        assert deployment_id
        assert process.pid == 9876
        return {"status": "running"}

    async def _no_broker_views(
        self,
        rows: list[dict[str, Any]],
        runtime_by_id: dict[str, dict[str, Any]],
        now: Any,
    ) -> dict[str, Any]:
        assert runtime_by_id == {}
        return {}

    monkeypatch.setattr(TradingNodeManager, "_assert_no_live_overlap", _no_overlap)
    monkeypatch.setattr(TradingNodeManager, "_await_startup_status", _fake_startup_status)
    monkeypatch.setattr(TradingNodeManager, "_load_broker_views", _no_broker_views)

    captured: dict[str, Any] = {}

    class _FakeQueue:
        def get_nowait(self) -> Any:
            raise queue.Empty

        def close(self) -> None:
            return None

    class _FakeProcess:
        def __init__(self, *, name: str, target: Any, args: tuple[Any, ...], daemon: bool) -> None:
            self.name = name
            self.target = target
            self.args = args
            self.daemon = daemon
            self.pid = 9876
            self._alive = False

        def start(self) -> None:
            self._alive = True
            captured["payload"] = self.args[0]

        def is_alive(self) -> bool:
            return self._alive

        def terminate(self) -> None:
            self._alive = False

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            return None

    class _FakeContext:
        def Queue(self) -> _FakeQueue:
            return _FakeQueue()

        def Process(self, **kwargs: Any) -> _FakeProcess:
            process = _FakeProcess(**kwargs)
            captured["process"] = process
            return process

    monkeypatch.setattr(trading_node_module.mp, "get_context", lambda method: _FakeContext())

    async with session_factory() as session:
        user = User(
            id="user-1",
            entra_id="entra-user-1",
            email="user-1@example.com",
            display_name="User One",
            role="trader",
        )
        strategy_one = Strategy(
            id="strategy-1",
            name="example.mean_reversion",
            file_path="example/mean_reversion.py",
            strategy_class="MeanReversionZScoreStrategy",
        )
        strategy_two = Strategy(
            id="strategy-2",
            name="example.mean_reversion.alt",
            file_path="example/mean_reversion.py",
            strategy_class="MeanReversionZScoreStrategy",
        )
        portfolio = LivePortfolio(id="portfolio-1", name="Live Core")
        revision = LivePortfolioRevision(
            id="revision-1",
            portfolio_id=portfolio.id,
            revision_number=1,
            composition_hash="hash-revision-1",
            is_frozen=True,
        )
        member_one = LivePortfolioRevisionStrategy(
            id="member-1",
            revision_id=revision.id,
            strategy_id=strategy_one.id,
            config={
                "instrument_id": "SPY.XNAS",
                "bar_type": "SPY.XNAS-1-MINUTE-LAST-EXTERNAL",
                "trade_size": "1",
            },
            instruments=["SPY.XNAS"],
            weight=Decimal("1.0"),
            order_index=0,
        )
        member_two = LivePortfolioRevisionStrategy(
            id="member-2",
            revision_id=revision.id,
            strategy_id=strategy_two.id,
            config={
                "instrument_id": "QQQ.XNAS",
                "bar_type": "QQQ.XNAS-1-MINUTE-LAST-EXTERNAL",
                "trade_size": "2",
            },
            instruments=["QQQ.XNAS"],
            weight=Decimal("1.0"),
            order_index=1,
        )
        session.add_all([user, strategy_one, strategy_two, portfolio, revision, member_one, member_two])
        await session.commit()

    manager = TradingNodeManager()
    deployment_id = await manager.start(
        portfolio_revision_id="revision-1",
        strategy_members=[
            {
                "revision_strategy_id": "member-1",
                "strategy_id": "strategy-1",
                "strategy_name": "example.mean_reversion",
                "strategy_class": "MeanReversionZScoreStrategy",
                "strategy_code_hash": "hash-1",
                "strategy_path": "strategies.example.mean_reversion:MeanReversionZScoreStrategy",
                "config_path": "strategies.example.mean_reversion:MeanReversionZScoreConfig",
                "config": {
                    "instrument_id": "SPY.XNAS",
                    "bar_type": "SPY.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "trade_size": "1",
                },
                "instrument_ids": ["SPY.XNAS"],
                "order_index": 0,
            },
            {
                "revision_strategy_id": "member-2",
                "strategy_id": "strategy-2",
                "strategy_name": "example.mean_reversion.alt",
                "strategy_class": "MeanReversionZScoreStrategy",
                "strategy_code_hash": "hash-2",
                "strategy_path": "strategies.example.mean_reversion:MeanReversionZScoreStrategy",
                "config_path": "strategies.example.mean_reversion:MeanReversionZScoreConfig",
                "config": {
                    "instrument_id": "QQQ.XNAS",
                    "bar_type": "QQQ.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "trade_size": "2",
                },
                "instrument_ids": ["QQQ.XNAS"],
                "order_index": 1,
            },
        ],
        instruments=["SPY.XNAS", "QQQ.XNAS"],
        paper_trading=True,
        started_by="user-1",
        account_id="DU123456",
        identity_signature="portfolio:revision-1:DU123456",
    )

    assert deployment_id
    assert captured["payload"].portfolio_revision_id == "revision-1"
    assert len(captured["payload"].strategy_members) == 2

    async with session_factory() as session:
        deployment = await session.get(LiveDeployment, deployment_id)
        assert deployment is not None
        assert deployment.portfolio_revision_id == "revision-1"
        assert deployment.strategy_id is None
        assert deployment.account_id == "DU123456"
        assert deployment.status == "running"
        bridge_rows = (
            await session.execute(
                select(LiveDeploymentStrategy).where(LiveDeploymentStrategy.deployment_id == deployment_id)
            )
        ).scalars().all()
        assert len(bridge_rows) == 2
        assert {row.revision_strategy_id for row in bridge_rows} == {"member-1", "member-2"}
        assert any("-0-" in row.strategy_id_full for row in bridge_rows)
        assert any("-1-" in row.strategy_id_full for row in bridge_rows)

    status_rows = await manager.status()
    assert len(status_rows) == 1
    assert status_rows[0]["portfolio_revision_id"] == "revision-1"
    assert len(status_rows[0]["members"]) == 2
    assert [member["order_index"] for member in status_rows[0]["members"]] == [0, 1]

    await engine.dispose()


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    _ = args, kwargs


async def _async_empty_dict() -> dict[str, dict[str, Any]]:
    return {}
