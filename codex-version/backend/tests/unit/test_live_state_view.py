from msai.services.live_state_view import (
    build_positions_payload,
    build_risk_payload,
    build_status_payload,
    build_trades_payload,
)


def test_build_status_payload_merges_runtime_snapshot() -> None:
    rows = [
        {
            "id": "dep-1",
            "strategy_id": "strategy-1",
            "status": "running",
            "started_at": "2026-04-06T10:00:00+00:00",
            "process_alive": True,
            "control_mode": "local",
            "runtime_fresh": True,
            "paper_trading": True,
        }
    ]
    payload = build_status_payload(
        rows,
        {"strategy-1": "example.ema_cross"},
        [
            {
                "scope": "dep-1",
                "data": {
                    "status": "running",
                    "daily_pnl": 12.5,
                    "open_positions": 1,
                    "open_orders": 2,
                    "updated_at": "2026-04-06T10:05:00+00:00",
                    "reason": "controller_started",
                },
            }
        ],
    )

    assert payload[0]["strategy"] == "example.ema_cross"
    assert payload[0]["daily_pnl"] == 12.5
    assert payload[0]["open_positions"] == 1
    assert payload[0]["open_orders"] == 2
    assert payload[0]["control_mode"] == "local"
    assert payload[0]["reason"] == "controller_started"
    assert payload[0]["runtime_fresh"] is True


def test_build_status_payload_prefers_runtime_blocked_status() -> None:
    payload = build_status_payload(
        [
            {
                "id": "dep-1",
                "strategy_id": "strategy-1",
                "status": "starting",
                "started_at": "2026-04-06T10:00:00+00:00",
                "process_alive": False,
                "control_mode": "none",
                "runtime_fresh": True,
                "paper_trading": True,
            }
        ],
        {"strategy-1": "example.ema_cross"},
        [
            {
                "scope": "dep-1",
                "data": {
                    "status": "blocked",
                    "reason": "daily loss threshold breached",
                },
            }
        ],
    )

    assert payload[0]["status"] == "blocked"
    assert payload[0]["control_mode"] == "none"
    assert payload[0]["reason"] == "daily loss threshold breached"


def test_build_status_payload_preserves_reconciliation_state_when_runtime_is_stale() -> None:
    payload = build_status_payload(
        [
            {
                "id": "dep-1",
                "strategy_id": "strategy-1",
                "status": "orphaned_exposure",
                "started_at": "2026-04-06T10:00:00+00:00",
                "process_alive": False,
                "control_mode": "broker",
                "runtime_fresh": False,
                "paper_trading": False,
                "reason": "Broker exposure remains without a fresh Nautilus runtime snapshot",
                "broker_connected": True,
                "broker_mock_mode": False,
                "broker_updated_at": "2026-04-06T10:07:00+00:00",
                "broker_open_positions": 2,
                "broker_open_orders": 1,
                "broker_exposure_detected": True,
            }
        ],
        {"strategy-1": "example.ema_cross"},
        [
            {
                "scope": "dep-1",
                "data": {
                    "status": "running",
                    "daily_pnl": 12.5,
                    "open_positions": 9,
                    "open_orders": 9,
                    "updated_at": "2026-04-06T10:04:00+00:00",
                    "reason": "stale_runtime_snapshot",
                },
            }
        ],
    )

    assert payload[0]["status"] == "orphaned_exposure"
    assert payload[0]["open_positions"] == 2
    assert payload[0]["open_orders"] == 1
    assert payload[0]["daily_pnl"] == 0.0
    assert payload[0]["reason"] == "Broker exposure remains without a fresh Nautilus runtime snapshot"
    assert payload[0]["updated_at"] == "2026-04-06T10:07:00+00:00"
    assert payload[0]["broker_exposure_detected"] is True


def test_build_status_payload_ignores_runtime_counts_for_stopped_deployments() -> None:
    payload = build_status_payload(
        [
            {
                "id": "dep-1",
                "strategy_id": "strategy-1",
                "status": "stopped",
                "started_at": "2026-04-06T10:00:00+00:00",
                "process_alive": False,
                "control_mode": "none",
                "runtime_fresh": True,
                "paper_trading": True,
                "broker_open_positions": 0,
                "broker_open_orders": 0,
            }
        ],
        {"strategy-1": "example.ema_cross"},
        [
            {
                "scope": "dep-1",
                "data": {
                    "status": "stopped",
                    "daily_pnl": 0.0,
                    "open_positions": 1,
                    "open_orders": 2,
                    "updated_at": "2026-04-06T10:05:00+00:00",
                    "reason": "controller_stopped",
                },
            }
        ],
    )

    assert payload[0]["status"] == "stopped"
    assert payload[0]["open_positions"] == 0
    assert payload[0]["open_orders"] == 0
    assert payload[0]["reason"] == "controller_stopped"


def test_build_positions_payload_filters_inactive_and_mode() -> None:
    snapshots = [
        {
            "scope": "dep-1",
            "data": [
                {
                    "deployment_id": "dep-1",
                    "paper_trading": True,
                    "instrument": "AAPL.XNAS",
                    "quantity": 1.0,
                }
            ],
        },
        {
            "scope": "dep-2",
            "data": [
                {
                    "deployment_id": "dep-2",
                    "paper_trading": False,
                    "instrument": "MSFT.XNAS",
                    "quantity": 2.0,
                }
            ],
        },
    ]

    payload = build_positions_payload(
        snapshots,
        active_deployments={"dep-1"},
        paper_trading=True,
    )

    assert payload == [
        {
            "deployment_id": "dep-1",
            "paper_trading": True,
            "instrument": "AAPL.XNAS",
            "quantity": 1.0,
        }
    ]


def test_build_trades_payload_sorts_latest_first() -> None:
    snapshots = [
        {
            "scope": "dep-1",
            "data": [
                {"id": "trade-1", "executed_at": "2026-04-06T10:00:00+00:00"},
                {"id": "trade-2", "executed_at": "2026-04-06T10:05:00+00:00"},
            ],
        }
    ]

    payload = build_trades_payload(snapshots, active_deployments={"dep-1"})

    assert [row["id"] for row in payload] == ["trade-2", "trade-1"]


def test_build_risk_payload_aggregates_snapshot_metrics() -> None:
    payload = build_risk_payload(
        {"halted": True, "reason": "kill switch", "updated_at": "2026-04-06T10:00:00+00:00"},
        [
            {
                "scope": "dep-1",
                "generated_at": "2026-04-06T10:02:00+00:00",
                "data": {
                    "current_pnl": 10.0,
                    "notional_exposure": 100.0,
                    "portfolio_value": 1000.0,
                    "margin_used": 25.0,
                    "position_count": 1,
                },
            },
            {
                "scope": "dep-2",
                "generated_at": "2026-04-06T10:03:00+00:00",
                "data": {
                    "current_pnl": -3.0,
                    "notional_exposure": 50.0,
                    "portfolio_value": 500.0,
                    "margin_used": 10.0,
                    "position_count": 2,
                },
            },
        ],
        active_deployments={"dep-1", "dep-2"},
    )

    assert payload["halted"] is True
    assert payload["current_pnl"] == 7.0
    assert payload["notional_exposure"] == 150.0
    assert payload["portfolio_value"] == 1500.0
    assert payload["margin_used"] == 35.0
    assert payload["position_count"] == 3
    assert payload["updated_at"] == "2026-04-06T10:03:00+00:00"


def test_build_risk_payload_parses_currency_suffixed_snapshot_values() -> None:
    payload = build_risk_payload(
        {"halted": False, "reason": None, "updated_at": "2026-04-06T10:00:00+00:00"},
        [
            {
                "scope": "dep-1",
                "generated_at": "2026-04-06T10:02:00+00:00",
                "data": {
                    "current_pnl": "-4.10 USD",
                    "notional_exposure": "0.00 USD",
                    "portfolio_value": "999,995.90 USD",
                    "margin_used": "0.00 USD",
                    "position_count": 0,
                },
            }
        ],
        active_deployments={"dep-1"},
    )

    assert payload["current_pnl"] == -4.10
    assert payload["portfolio_value"] == 999_995.90
