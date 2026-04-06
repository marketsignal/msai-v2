from msai.services.nautilus.trading_node import _TradingNodePayload, build_trading_node_config


def test_build_trading_node_config_has_ib_clients() -> None:
    payload = _TradingNodePayload(
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        config_path="strategies.example.config:EMACrossConfig",
        config={
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
            "trade_size": "1",
        },
        ibg_host="ib-gateway",
        ibg_port=4002,
        data_client_id=11,
        exec_client_id=12,
        account_id="DU123456",
        trader_id="TRADER-001",
    )

    config = build_trading_node_config(payload)
    assert "IB" in config.data_clients
    assert "IB" in config.exec_clients
    assert config.strategies[0].strategy_path == payload.strategy_path
