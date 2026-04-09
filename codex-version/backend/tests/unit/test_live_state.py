import pytest

from msai.services.nautilus import live_state as live_state_module
from msai.services.nautilus.live_state import (
    LiveStateController,
    LiveStateControllerConfig,
    _decode_liquidation_command,
    _order_event_payload,
    _order_event_update_type,
    _snapshot_is_flat,
    _to_float,
)


def test_live_state_controller_init_uses_inherited_config_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_controller_init(self, trader, config) -> None:
        assert trader == "trader"
        object.__setattr__(self, "_base_config", config)

    monkeypatch.setattr(live_state_module.Controller, "__init__", _fake_controller_init)

    config = LiveStateControllerConfig(
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_name="example.ema_cross",
        strategy_code_hash="hash-1",
        instrument_ids=("AAPL.XNAS",),
        startup_instrument_id="AAPL.XNAS",
    )

    controller = LiveStateController("trader", config=config)

    assert controller._timer_name == "live-state:dep-1"
    assert controller._base_config == config


def test_decode_liquidation_command_parses_shutdown_flag() -> None:
    command = _decode_liquidation_command(
        '{"action":"liquidate","reason":"manual kill switch","shutdown_after_flat":true}'
    )

    assert command == {
        "action": "liquidate",
        "reason": "manual kill switch",
        "shutdown_after_flat": True,
    }


def test_decode_liquidation_command_accepts_structured_payload() -> None:
    command = _decode_liquidation_command(
        {
            "action": "liquidate",
            "reason": "structured control command",
            "shutdown_after_flat": False,
        }
    )

    assert command == {
        "action": "liquidate",
        "reason": "structured control command",
        "shutdown_after_flat": False,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"reason":"missing action"}', "Unsupported liquidation action: missing"),
        ('{"action":"pause"}', "Unsupported liquidation action: pause"),
        ('["not","an","object"]', "Liquidation payload must decode to an object"),
    ],
)
def test_decode_liquidation_command_rejects_invalid_payloads(payload: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _decode_liquidation_command(payload)


def test_snapshot_is_flat_requires_no_positions_and_no_open_orders() -> None:
    assert _snapshot_is_flat({"positions": [], "orders": []}) is True
    assert _snapshot_is_flat({"positions": [{"position_id": "p-1"}], "orders": []}) is False
    assert _snapshot_is_flat({"positions": [], "orders": [{"client_order_id": "o-1", "is_closed": False}]}) is False
    assert _snapshot_is_flat({"positions": [], "orders": [{"client_order_id": "o-2", "is_closed": True}]}) is True


def test_order_event_update_type_uses_snake_case_suffix() -> None:
    assert _order_event_update_type("OrderPendingCancel") == "order.pending_cancel"
    assert _order_event_update_type("OrderModifyRejected") == "order.modify_rejected"


def test_order_event_payload_extracts_common_fields() -> None:
    class _Value:
        def __init__(self, value: str) -> None:
            self.value = value

    class OrderAccepted:
        ts_event = 1_710_000_000_000_000_000
        id = _Value("evt-1")
        instrument_id = _Value("AAPL.XNAS")
        client_order_id = _Value("ord-1")
        venue_order_id = _Value("venue-1")
        account_id = _Value("DU123456")

        @staticmethod
        def to_dict(_event) -> dict[str, object]:
            return {
                "event_id": "evt-1",
                "instrument_id": "AAPL.XNAS",
                "client_order_id": "ord-1",
                "venue_order_id": "venue-1",
                "account_id": "DU123456",
            }

    payload = _order_event_payload(
        OrderAccepted(),
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_code_hash="hash-1",
        paper_trading=True,
    )

    assert payload["deployment_id"] == "dep-1"
    assert payload["strategy_id"] == "strategy-1"
    assert payload["event_id"] == "evt-1"
    assert payload["event_type"] == "OrderAccepted"
    assert payload["instrument"] == "AAPL.XNAS"
    assert payload["client_order_id"] == "ord-1"
    assert payload["venue_order_id"] == "venue-1"
    assert payload["account_id"] == "DU123456"
    assert payload["payload"]["event_id"] == "evt-1"


def test_order_event_payload_sanitizes_non_json_fields() -> None:
    class _Value:
        def __init__(self, value: str) -> None:
            self.value = value

    class _Money:
        def __init__(self, value: str) -> None:
            self.value = value

    class OrderFilled:
        ts_event = 1_710_000_000_000_000_000
        id = _Value("evt-2")
        instrument_id = _Value("EUR/USD.IDEALPRO")
        client_order_id = _Value("ord-2")
        venue_order_id = _Value("venue-2")
        account_id = _Value("DU123456")

        @staticmethod
        def to_dict(_event) -> dict[str, object]:
            return {
                "event_id": "evt-2",
                "instrument_id": "EUR/USD.IDEALPRO",
                "client_order_id": "ord-2",
                "venue_order_id": "venue-2",
                "account_id": "DU123456",
                "last_px": _Money("1.16615"),
                "commission": _Money("2.00 USD"),
                "nested": {"price": _Money("1.16635")},
            }

    payload = _order_event_payload(
        OrderFilled(),
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_code_hash="hash-1",
        paper_trading=True,
    )

    assert payload["payload"]["last_px"] == "1.16615"
    assert payload["payload"]["commission"] == "2.00 USD"
    assert payload["payload"]["nested"]["price"] == "1.16635"


def test_to_float_parses_currency_suffixed_values() -> None:
    assert _to_float("2.00 USD") == 2.0
    assert _to_float("1,000,000.00 USD") == 1_000_000.0
    assert _to_float(None) is None


def test_schedule_task_uses_stored_loop_when_called_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_controller_init(self, trader, config) -> None:
        assert trader == "trader"
        object.__setattr__(self, "_base_config", config)

    monkeypatch.setattr(live_state_module.Controller, "__init__", _fake_controller_init)

    config = LiveStateControllerConfig(
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_name="example.ema_cross",
        strategy_code_hash="hash-1",
        instrument_ids=("AAPL.XNAS",),
        startup_instrument_id="AAPL.XNAS",
    )
    controller = LiveStateController("trader", config=config)
    class _StoredLoop:
        @staticmethod
        def is_running() -> bool:
            return True

    loop = _StoredLoop()
    controller._event_loop = loop

    class _FakeFuture:
        def add_done_callback(self, callback) -> None:
            callback(self)

        def exception(self) -> None:
            return None

    captured: dict[str, object] = {}

    def _raise_no_loop() -> None:
        raise RuntimeError

    def _fake_run_coroutine_threadsafe(coro, active_loop):
        captured["loop"] = active_loop
        coro.close()
        return _FakeFuture()

    monkeypatch.setattr(live_state_module.asyncio, "get_running_loop", _raise_no_loop)
    monkeypatch.setattr(
        live_state_module.asyncio,
        "run_coroutine_threadsafe",
        _fake_run_coroutine_threadsafe,
    )

    async def _sample() -> None:
        return None

    controller._schedule_task(_sample(), "snapshot-timer")

    assert captured["loop"] is loop


def test_on_stop_publishes_flat_terminal_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_controller_init(self, trader, config) -> None:
        assert trader == "trader"
        object.__setattr__(self, "_base_config", config)

    monkeypatch.setattr(live_state_module.Controller, "__init__", _fake_controller_init)
    monkeypatch.setattr(LiveStateController, "config", property(lambda self: self._base_config))

    config = LiveStateControllerConfig(
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_name="example.ema_cross",
        strategy_code_hash="hash-1",
        instrument_ids=("AAPL.XNAS",),
        startup_instrument_id="AAPL.XNAS",
    )
    controller = LiveStateController("trader", config=config)

    class _Clock:
        timer_names: set[str] = set()

        @staticmethod
        def cancel_timer(_name: str) -> None:
            return None

    monkeypatch.setattr(LiveStateController, "clock", property(lambda self: _Clock()))
    monkeypatch.setattr(controller, "_trade_rows", lambda: [{"id": "trade-1"}])
    monkeypatch.setattr(
        controller,
        "_risk_payload",
        lambda positions: {
            "deployment_id": "dep-1",
            "strategy_id": "strategy-1",
            "paper_trading": True,
            "current_pnl": 0.0,
            "notional_exposure": 0.0,
            "portfolio_value": 1000.0,
            "margin_used": 0.0,
            "position_count": len(positions),
            "currencies": [],
            "updated_at": "2026-04-08T22:55:13+00:00",
        },
    )
    monkeypatch.setattr(controller, "_resolve_live_strategy_id", lambda: None)
    captured: dict[str, object] = {}
    monkeypatch.setattr(controller, "_publish_runtime_snapshot_sync", lambda snapshot: captured.setdefault("snapshot", snapshot))

    controller.on_stop()

    snapshot = captured["snapshot"]
    assert snapshot["positions"] == []
    assert snapshot["orders"] == []
    assert snapshot["trades"] == [{"id": "trade-1"}]
    assert snapshot["status"]["status"] == "stopped"
    assert snapshot["status"]["open_positions"] == 0
    assert snapshot["status"]["open_orders"] == 0


@pytest.mark.asyncio
async def test_handle_order_event_continues_trade_fill_when_order_event_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_controller_init(self, trader, config) -> None:
        object.__setattr__(self, "_base_config", config)

    monkeypatch.setattr(live_state_module.Controller, "__init__", _fake_controller_init)
    monkeypatch.setattr(LiveStateController, "config", property(lambda self: self._base_config))

    config = LiveStateControllerConfig(
        deployment_id="dep-1",
        strategy_db_id="strategy-1",
        strategy_name="example.ema_cross",
        strategy_code_hash="hash-1",
        instrument_ids=("EUR/USD.IDEALPRO",),
        startup_instrument_id="EUR/USD.IDEALPRO",
    )
    controller = LiveStateController("trader", config=config)

    class _FakeFilled:
        pass

    monkeypatch.setattr(live_state_module, "OrderFilled", _FakeFilled)
    persisted: dict[str, object] = {}
    published: list[str] = []

    async def _raise_persist(_payload) -> None:
        raise TypeError("not json serializable")

    async def _persist_trade_fill(payload) -> None:
        persisted["trade"] = payload

    async def _publish_live_update(event_type, payload, *, scope=None) -> None:
        published.append(str(event_type))

    async def _publish_runtime_state(*, reason: str) -> None:
        persisted["reason"] = reason

    monkeypatch.setattr(live_state_module, "_order_event_payload", lambda *args, **kwargs: {
        "event_type": "OrderFilled",
        "event_id": "evt-1",
        "executed_at": "2026-04-08T22:46:50+00:00",
        "payload": {"event_id": "evt-1"},
    })
    monkeypatch.setattr(controller, "_persist_order_event", _raise_persist)
    monkeypatch.setattr(controller, "_fill_payload", lambda _event: {"broker_trade_id": "trade-1"})
    monkeypatch.setattr(controller, "_persist_trade_fill", _persist_trade_fill)
    monkeypatch.setattr(controller, "_publish_runtime_state", _publish_runtime_state)
    monkeypatch.setattr(live_state_module, "publish_live_update", _publish_live_update)

    await controller._handle_order_event(_FakeFilled())

    assert persisted["trade"] == {"broker_trade_id": "trade-1"}
    assert "trade.filled" in published
    assert persisted["reason"] == "order_event:OrderFilled"
