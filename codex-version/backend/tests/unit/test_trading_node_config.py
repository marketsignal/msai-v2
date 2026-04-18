import pytest

from msai.models import InstrumentDefinition
from msai.services.nautilus.trading_node import (
    DeploymentStopResult,
    TradingNodeManager,
    _broker_row_matches_instrument,
    _broker_view_is_flat,
    _BrokerExposureView,
    _deployment_broker_view,
    _deployment_liquidation_stream,
    _deployment_liquidation_topic,
    _deployment_shutdown_stream,
    _deployment_trader_id,
    _instrument_matcher,
    _next_ib_client_id_pair,
    _overlapping_live_deployments,
    _runtime_status_is_flat,
    _StrategyMemberPayload,
    _TradingNodePayload,
    build_trading_node_config,
)


def test_build_trading_node_config_has_ib_clients() -> None:
    payload = _TradingNodePayload(
        deployment_id="dep-1",
        deployment_slug="slug-1",
        strategy_id="strategy-1",
        strategy_name="example.ema_cross",
        strategy_code_hash="abc123",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        config_path="strategies.example.config:EMACrossConfig",
        config={
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
            "trade_size": "1",
        },
        order_id_tag="0-slug-1",
        strategy_id_full="EMACrossStrategy-0-slug-1",
        ibg_host="ib-gateway",
        ibg_port=4002,
        data_client_id=11,
        exec_client_id=12,
        account_id="DU123456",
        trader_id="TRADER-001",
        paper_trading=True,
        instrument_ids=("AAPL.XNAS",),
    )

    config = build_trading_node_config(payload)
    assert "IB" in config.data_clients
    assert "IB" in config.exec_clients
    assert config.strategies[0].strategy_path == payload.strategy_path
    assert config.cache is not None
    assert config.message_bus is not None
    assert config.controller is not None
    assert config.exec_engine.snapshot_positions is True
    assert config.controller.config["startup_instrument_id"] == "AAPL.XNAS"
    assert config.controller.config["startup_quantity"] == 1.0
    assert config.message_bus.external_streams == [
        _deployment_shutdown_stream(payload.trader_id),
        _deployment_liquidation_stream(payload.trader_id, payload.deployment_id),
    ]
    assert config.controller.config["account_id"] == "DU123456"
    assert config.controller.config["liquidation_topic"] == _deployment_liquidation_topic("dep-1")


def test_build_trading_node_config_supports_multi_strategy_payload() -> None:
    payload = _TradingNodePayload(
        deployment_id="dep-2",
        deployment_slug="slug-2",
        strategy_id="strategy-1",
        strategy_name="portfolio",
        strategy_code_hash="aggregate-hash",
        strategy_path="strategies.example.mean_reversion:MeanReversionZScoreStrategy",
        config_path="strategies.example.mean_reversion:MeanReversionZScoreConfig",
        config={
            "instrument_id": "SPY.XNAS",
            "bar_type": "SPY.XNAS-1-MINUTE-LAST-EXTERNAL",
            "trade_size": "1",
        },
        order_id_tag="0-slug-2",
        strategy_id_full="MeanReversionZScoreStrategy-0-slug-2",
        ibg_host="ib-gateway",
        ibg_port=4002,
        data_client_id=21,
        exec_client_id=22,
        account_id="DU654321",
        trader_id="TRADER-002",
        paper_trading=True,
        instrument_ids=("SPY.XNAS", "QQQ.XNAS"),
        portfolio_revision_id="revision-1",
        strategy_members=(
            _StrategyMemberPayload(
                revision_strategy_id="member-1",
                strategy_id="strategy-1",
                strategy_name="example.mean_reversion",
                strategy_code_hash="hash-1",
                strategy_path="strategies.example.mean_reversion:MeanReversionZScoreStrategy",
                config_path="strategies.example.mean_reversion:MeanReversionZScoreConfig",
                config={
                    "instrument_id": "SPY.XNAS",
                    "bar_type": "SPY.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "trade_size": "1",
                },
                order_id_tag="0-slug-2",
                strategy_id_full="MeanReversionZScoreStrategy-0-slug-2",
                instrument_ids=("SPY.XNAS",),
            ),
            _StrategyMemberPayload(
                revision_strategy_id="member-2",
                strategy_id="strategy-2",
                strategy_name="example.mean_reversion.two",
                strategy_code_hash="hash-2",
                strategy_path="strategies.example.mean_reversion:MeanReversionZScoreStrategy",
                config_path="strategies.example.mean_reversion:MeanReversionZScoreConfig",
                config={
                    "instrument_id": "QQQ.XNAS",
                    "bar_type": "QQQ.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "trade_size": "2",
                },
                order_id_tag="1-slug-2",
                strategy_id_full="MeanReversionZScoreStrategy-1-slug-2",
                instrument_ids=("QQQ.XNAS",),
            ),
        ),
    )

    config = build_trading_node_config(payload)

    assert len(config.strategies) == 2
    assert config.controller.config["portfolio_revision_id"] == "revision-1"
    assert len(config.controller.config["strategy_members"]) == 2
    assert config.controller.config["startup_quantity"] == 3.0


def test_deployment_trader_id_is_deterministic() -> None:
    deployment_id = "12345678-9abc-def0-1234-56789abcdef0"

    assert _deployment_trader_id(deployment_id) == "TRADER-001-123456789ABC"


def test_deployment_liquidation_topic_is_scoped() -> None:
    deployment_id = "dep-123"

    assert _deployment_liquidation_topic(deployment_id) == "commands.msai.deployment.dep-123.liquidate"
    assert _deployment_liquidation_stream("TRADER-001", deployment_id) == (
        "trader-TRADER-001:stream:commands.msai.deployment.dep-123.liquidate"
    )


def test_instrument_matcher_uses_contract_details_aliases() -> None:
    definition = InstrumentDefinition(
        instrument_id="AAPL.XNAS",
        raw_symbol="AAPL",
        venue="XNAS",
        instrument_type="Equity",
        security_type="STK",
        asset_class="stocks",
        instrument_data={"id": "AAPL.XNAS"},
        contract_details={
            "contract": {
                "conId": 265598,
                "symbol": "AAPL",
                "localSymbol": "AAPL",
                "tradingClass": "NMS",
            }
        },
    )

    matcher = _instrument_matcher("AAPL.XNAS", definition)

    assert _broker_row_matches_instrument(
        {
            "instrument": "AAPL",
            "symbol": "AAPL",
            "local_symbol": "AAPL",
            "con_id": 265598,
        },
        matcher,
    )


def test_deployment_broker_view_detects_orphaned_exposure_from_open_orders() -> None:
    definition = InstrumentDefinition(
        instrument_id="AAPL.XNAS",
        raw_symbol="AAPL",
        venue="XNAS",
        instrument_type="Equity",
        security_type="STK",
        asset_class="stocks",
        instrument_data={"id": "AAPL.XNAS"},
        contract_details={"contract": {"conId": 265598, "symbol": "AAPL", "localSymbol": "AAPL"}},
    )

    broker_view = _deployment_broker_view(
        {
            "id": "dep-1",
            "instruments": ["AAPL.XNAS"],
        },
        definitions_by_id={"AAPL.XNAS": definition},
        broker_state=type(
            "Snapshot",
            (),
            {
                "connected": True,
                "mock_mode": False,
                "generated_at": "2026-04-07T12:00:00+00:00",
                "positions": [],
                "open_orders": [
                    {
                        "instrument": "AAPL",
                        "symbol": "AAPL",
                        "local_symbol": "AAPL",
                        "con_id": 265598,
                    }
                ],
            },
        )(),
    )

    assert isinstance(broker_view, _BrokerExposureView)
    assert broker_view.exposure_detected is True
    assert broker_view.open_positions == 0
    assert broker_view.open_orders == 1


def test_deployment_broker_view_prefers_exec_client_id_for_open_orders() -> None:
    definition = InstrumentDefinition(
        instrument_id="AAPL.XNAS",
        raw_symbol="AAPL",
        venue="XNAS",
        instrument_type="Equity",
        security_type="STK",
        asset_class="stocks",
        instrument_data={"id": "AAPL.XNAS"},
        contract_details={"contract": {"conId": 265598, "symbol": "AAPL", "localSymbol": "AAPL"}},
    )

    broker_view = _deployment_broker_view(
        {
            "id": "dep-1",
            "ib_exec_client_id": 18,
            "instruments": ["AAPL.XNAS"],
        },
        definitions_by_id={"AAPL.XNAS": definition},
        broker_state=type(
            "Snapshot",
            (),
            {
                "connected": True,
                "mock_mode": False,
                "generated_at": "2026-04-07T12:00:00+00:00",
                "positions": [],
                "open_orders": [
                    {
                        "instrument": "AAPL",
                        "symbol": "AAPL",
                        "local_symbol": "AAPL",
                        "con_id": 265598,
                        "client_id": 99,
                    },
                    {
                        "instrument": "MSFT",
                        "symbol": "MSFT",
                        "local_symbol": "MSFT",
                        "con_id": 272093,
                        "client_id": 18,
                    },
                ],
            },
        )(),
    )

    assert broker_view.open_orders == 1
    assert broker_view.exposure_detected is True


def test_broker_view_is_flat_requires_connected_state_without_exposure() -> None:
    assert _broker_view_is_flat(
        _BrokerExposureView(
            connected=True,
            mock_mode=False,
            generated_at="2026-04-08T22:52:25+00:00",
            open_positions=0,
            open_orders=0,
            exposure_detected=False,
            reason=None,
        )
    )
    assert not _broker_view_is_flat(
        _BrokerExposureView(
            connected=None,
            mock_mode=False,
            generated_at=None,
            open_positions=0,
            open_orders=0,
            exposure_detected=False,
            reason=None,
        )
    )
    assert not _broker_view_is_flat(
        _BrokerExposureView(
            connected=True,
            mock_mode=False,
            generated_at="2026-04-08T22:52:25+00:00",
            open_positions=0,
            open_orders=1,
            exposure_detected=True,
            reason=None,
        )
    )


def test_next_ib_client_id_pair_skips_active_allocations() -> None:
    data_client_id, exec_client_id = _next_ib_client_id_pair(
        [
            {"status": "running", "ib_data_client_id": 11, "ib_exec_client_id": 12},
            {"status": "stopped", "ib_data_client_id": 13, "ib_exec_client_id": 14},
            {"status": "orphaned_exposure", "ib_data_client_id": 15, "ib_exec_client_id": 16},
        ]
    )

    assert (data_client_id, exec_client_id) == (13, 14)


def test_overlapping_live_deployments_blocks_same_mode_instruments() -> None:
    conflicts = _overlapping_live_deployments(
        [
            {
                "id": "dep-1",
                "status": "running",
                "paper_trading": True,
                "instruments": ["AAPL.XNAS", "MSFT.XNAS"],
            },
            {
                "id": "dep-2",
                "status": "unmanaged",
                "paper_trading": True,
                "instruments": ["NVDA.XNAS"],
            },
            {
                "id": "dep-3",
                "status": "running",
                "paper_trading": False,
                "instruments": ["AAPL.XNAS"],
            },
        ],
        instruments=["AAPL.XNAS", "NVDA.XNAS"],
        paper_trading=True,
    )

    assert conflicts == [("dep-1", ["AAPL.XNAS"])]


def test_runtime_status_is_flat_uses_open_counts() -> None:
    assert _runtime_status_is_flat({"open_positions": 0, "open_orders": 0}) is True
    assert _runtime_status_is_flat({"open_positions": 1, "open_orders": 0}) is False
    assert _runtime_status_is_flat({"open_positions": 0, "open_orders": 2}) is False
    assert _runtime_status_is_flat(None) is False


@pytest.mark.asyncio
async def test_kill_all_finalizes_flat_unmanaged_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TradingNodeManager()
    finalized: list[str] = []
    liquidated: list[str] = []

    async def _fake_status() -> list[dict[str, object]]:
        return [
            {
                "id": "dep-flat",
                "status": "unmanaged",
                "broker_exposure_detected": False,
            },
            {
                "id": "dep-running",
                "status": "running",
                "broker_exposure_detected": False,
            },
            {
                "id": "dep-error",
                "status": "error",
                "broker_exposure_detected": False,
            },
        ]

    async def _fake_finalize(deployment_id: str) -> None:
        finalized.append(deployment_id)

    async def _fake_liquidate(deployment_id: str, *, reason: str) -> DeploymentStopResult:
        liquidated.append(deployment_id)
        assert "Global kill-all liquidation requested" in reason
        return DeploymentStopResult(found=True, stopped=True, reason="stopped")

    monkeypatch.setattr(manager, "status", _fake_status)
    monkeypatch.setattr(manager, "_finalize_flat_unmanaged_deployment", _fake_finalize)
    monkeypatch.setattr(manager, "liquidate_and_stop", _fake_liquidate)

    stopped = await manager.kill_all()

    assert stopped == 2
    assert finalized == ["dep-flat"]
    assert liquidated == ["dep-running"]
