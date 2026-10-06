"""Unit tests for ``msai.services.nautilus.backtest_runner``.

These tests exercise only the in-process config-builder pieces of the
runner.  They deliberately do NOT spin up an actual ``BacktestNode``
subprocess because that requires a populated Nautilus catalog plus a
sizeable chunk of CPU time -- the end-to-end path is covered by the
integration smoke test in Docker.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from msai.services.nautilus.backtest_runner import (
    _build_backtest_run_config,
    _build_strategy_config,
    _extract_metrics,
    _extract_venues_from_instrument_ids,
    _RunPayload,
)

if TYPE_CHECKING:
    from nautilus_trader.config import BacktestRunConfig

_STRATEGY_FILE = Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"


class TestBuildBacktestRunConfig:
    """Tests for the private ``_build_backtest_run_config`` helper."""

    def test_config_wires_importable_strategy_paths(self) -> None:
        """The built config carries the resolved strategy and config paths."""
        # Arrange
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.SIM",
                "bar_type": "AAPL.SIM-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 30,
                "trade_size": "1",
            },
            instrument_ids=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-02-01",
            catalog_path="./data/nautilus",
        )

        # Act
        run_config = _build_backtest_run_config(payload)

        # Assert
        strategy = _build_strategy_config(payload)
        data = run_config.data[0]

        assert strategy.strategy_path.endswith(":EMACrossStrategy")
        assert strategy.config_path.endswith(":EMACrossConfig")
        assert [str(value) for value in data.instrument_ids] == payload.instrument_ids
        assert data.bar_types == [strategy.config["bar_type"]]
        assert data.catalog_path == payload.catalog_path
        assert run_config.start == data.start_time == 1704067200000000000
        assert run_config.end == data.end_time == 1706831999999999999
        assert run_config.raise_exception is True

    def test_venue_is_sim(self) -> None:
        """The backtest config declares the SIM venue with a starting balance."""
        # Arrange
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.SIM",
                "bar_type": "AAPL.SIM-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )

        # Act
        run_config = _build_backtest_run_config(payload)

        # Assert
        assert len(run_config.venues) == 1
        venue = run_config.venues[0]
        assert venue.name == "SIM"
        assert venue.starting_balances[0].endswith("USD")


class TestExtractVenuesFromInstrumentIds:
    """Phase 2 task 2.9: derive per-backtest venue list from the
    canonical instrument IDs in the payload so the runner builds
    one ``BacktestVenueConfig`` per unique venue."""

    def test_single_venue_equity(self) -> None:
        assert _extract_venues_from_instrument_ids(["AAPL.NASDAQ"]) == ["NASDAQ"]

    def test_duplicates_collapse(self) -> None:
        assert _extract_venues_from_instrument_ids(["AAPL.NASDAQ", "MSFT.NASDAQ"]) == ["NASDAQ"]

    def test_multi_venue_preserves_first_seen_order(self) -> None:
        """A mixed backtest (equity + futures) produces both venues
        in first-seen order — deterministic so tests can assert on
        the resulting config list."""
        result = _extract_venues_from_instrument_ids(["AAPL.NASDAQ", "ESM5.CME", "MSFT.NASDAQ"])
        assert result == ["NASDAQ", "CME"]

    def test_option_venue_is_last_dot_component(self) -> None:
        """Option ids have internal spaces + multiple dots
        (``"C AAPL 20260515 150.SMART"``). The venue is everything
        after the FINAL ``.``."""
        assert _extract_venues_from_instrument_ids(["C AAPL 20260515 150.SMART"]) == ["SMART"]

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            _extract_venues_from_instrument_ids([])

    def test_missing_venue_suffix_raises(self) -> None:
        """A bare ticker without the ``.VENUE`` suffix is a migration
        bug — we reject at config-build time rather than letting
        Nautilus crash mid-backtest."""
        with pytest.raises(ValueError, match="venue suffix"):
            _extract_venues_from_instrument_ids(["AAPL"])


class TestMultiVenueBuildConfig:
    def test_multi_venue_produces_one_config_per_venue(self) -> None:
        """A backtest spanning ``["AAPL.NASDAQ", "ESM5.CME"]`` gets
        TWO ``BacktestVenueConfig`` entries — one per unique
        venue — so Nautilus's engine can route orders to the
        correct simulated venue."""
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ", "ESM5.CME"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )
        run_config = _build_backtest_run_config(payload)

        venue_names = sorted(v.name for v in run_config.venues)
        assert venue_names == ["CME", "NASDAQ"]
        assert run_config.data[0].bar_types == [
            "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "ESM5.CME-1-MINUTE-LAST-EXTERNAL",
        ]


def _window_config(start: str, end: str) -> BacktestRunConfig:
    return _build_backtest_run_config(
        _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ"],
            start_date=start,
            end_date=end,
            catalog_path="./data/nautilus",
        )
    )


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        ("2024-02-29", "2024-02-29", 1709164800000000000, 1709251199999999999),
        ("2024-12-31", "2025-01-01", 1735603200000000000, 1735775999999999999),
        ("1970-01-01", "1970-01-01", 0, 86399999999999),
        (
            "2024-12-03T12:00:00.000000001Z",
            "2024-12-03T15:00:00.000000002Z",
            1733227200000000001,
            1733238000000000002,
        ),
        (
            "2024-12-03T07:00:00-05:00",
            "2024-12-03",
            1733227200000000000,
            1733270399999999999,
        ),
        (
            "2024-12-03",
            "2024-12-03T07:00:00-05:00",
            1733184000000000000,
            1733227200000000000,
        ),
    ],
)
def test_execution_bounds_preserve_utc_dates_and_exact_instants(
    start: str, end: str, expected_start: int, expected_end: int
) -> None:
    config = _window_config(start, end)
    assert config.start == config.data[0].start_time == expected_start
    assert config.end == config.data[0].end_time == expected_end


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2024-12-03", "2024-12-02", "End date must be on or after start date"),
        ("2024-12-03T13:00:00Z", "2024-12-03T12:00:00Z", "End date must be"),
        ("2024-02-30", "2024-03-01", "Invalid backtest start"),
        ("20241203", "2024-12-04", "Invalid backtest start"),
        ("2024-W49-2", "2024-12-04", "Invalid backtest start"),
        ("2024-12-03", "not-a-date", "Invalid backtest end"),
        ("1969-12-31", "1970-01-01", "supported nanosecond range"),
        ("2262-04-11", "2262-04-11", "supported nanosecond range"),
        ("9999-12-31", "9999-12-31", "supported nanosecond range"),
    ],
)
def test_invalid_or_unrepresentable_window_fails_before_engine(
    start: str, end: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _window_config(start, end)


@pytest.mark.parametrize("count", [0, 3])
def test_metrics_expose_actual_native_bar_count_including_zero(count: int) -> None:
    metrics = _extract_metrics(SimpleNamespace(iterations=count), pd.DataFrame())
    assert metrics["num_bars"] == count
    assert isinstance(metrics["num_bars"], int)


def test_metrics_without_native_count_do_not_fabricate_zero() -> None:
    assert "num_bars" not in _extract_metrics(SimpleNamespace(), pd.DataFrame())


@pytest.mark.parametrize("failure", ["empty", "zero", "report", "exception", "shared_capital"])
def test_native_failure_never_becomes_zero_success(tmp_path, monkeypatch, failure):
    from msai.services.nautilus import backtest_runner as runner

    calls = []

    class Node:
        def __init__(self, configs):
            calls.append("create")

        def build(self):
            calls.append("build")

        def add_strategy_from_config(self, identity, config):
            calls.append("strategy")

        def run(self):
            if failure == "exception":
                raise RuntimeError("native execution error")
            return (
                []
                if failure == "empty"
                else [SimpleNamespace(iterations=0 if failure == "zero" else 2)]
            )

        def generate_orders_report(self, identity):
            return None

        def dispose(self):
            calls.append("dispose")

    monkeypatch.setattr(runner, "BacktestNode", Node)
    payload = _RunPayload(
        strategy_file=str(_STRATEGY_FILE),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=str(tmp_path),
        result_path=str(tmp_path / "result.pkl"),
    )
    if failure == "shared_capital":
        payload.strategy_config["shared_capital"] = True
    runner._run_in_subprocess(payload)
    with open(payload.result_path, "rb") as handle:
        result = pickle.load(handle)
    assert result["ok"] is False
    assert "metrics" not in result
    if failure == "exception":
        assert "native execution error" in result["error"]
    if failure == "shared_capital":
        assert calls == []
        assert "shared_capital" in result["error"]
    else:
        assert calls[-1] == "dispose"


def test_timeout_terminates_spawned_child_and_cleans_ipc(tmp_path, monkeypatch):
    from msai.services.nautilus import backtest_runner as runner

    calls = []

    class Process:
        def start(self):
            calls.append("start")

        def join(self, timeout):
            calls.append(("join", timeout))

        def is_alive(self):
            return True

        def terminate(self):
            calls.append("terminate")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        runner.mp, "get_context", lambda mode: SimpleNamespace(Process=lambda **kwargs: Process())
    )
    monkeypatch.setattr(runner.tempfile, "tempdir", str(tmp_path))
    with pytest.raises(TimeoutError):
        runner.BacktestRunner().run(
            str(_STRATEGY_FILE),
            {},
            ["AAPL.NASDAQ"],
            "2025-01-02",
            "2025-01-02",
            tmp_path,
            timeout_seconds=1,
        )
    assert calls == ["start", ("join", 1), "terminate", ("join", 5), "close"]
    assert list(tmp_path.iterdir()) == []


def test_incomplete_child_payload_is_a_failure(tmp_path, monkeypatch):
    from msai.services.nautilus import backtest_runner as runner

    class Process:
        def __init__(self, target, args):
            self.payload = args[0]

        def start(self):
            with open(self.payload.result_path, "wb") as handle:
                pickle.dump({"ok": True}, handle)

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(runner.mp, "get_context", lambda mode: SimpleNamespace(Process=Process))
    with pytest.raises(RuntimeError, match="Incomplete"):
        runner.BacktestRunner().run(
            str(_STRATEGY_FILE), {}, ["AAPL.NASDAQ"], "2025-01-02", "2025-01-02", tmp_path
        )


def test_callback_capture_restores_inherited_methods_and_first_error(monkeypatch):
    from msai.services.nautilus import backtest_runner as runner

    class Parent:
        def on_bar(self):
            raise ValueError("first failure")

    class Child(Parent):
        def on_stop(self):
            raise ValueError("later failure")

    original = Child.on_stop
    monkeypatch.setattr(
        runner,
        "resolve_importable_strategy_paths",
        lambda path: SimpleNamespace(strategy_path="fixture:Child"),
    )
    monkeypatch.setattr(
        runner.importlib, "import_module", lambda module: SimpleNamespace(Child=Child)
    )
    with runner._capture_strategy_callback_failures("fixture") as failures:
        with pytest.raises(ValueError, match="first failure"):
            Child().on_bar()
        with pytest.raises(ValueError, match="later failure"):
            Child().on_stop()
    assert len(failures) == 1
    assert "first failure" in failures[0]
    assert Child.on_stop is original
    assert Child.on_bar is Parent.on_bar
    assert "on_bar" not in Child.__dict__


@pytest.mark.parametrize(
    "change",
    [
        {"instrument_id": "AAPL"},
        {"instrument_id": "MSFT.NASDAQ"},
        {"instrument_id": None},
        {"bar_type": "bad"},
        {"bar_type": None},
        {"bar_type": "MSFT.NASDAQ-1-MINUTE-LAST-EXTERNAL"},
        {"bar_type": "AAPL.NASDAQ-5-MINUTE-LAST-EXTERNAL"},
        {"bar_type": "AAPL.NASDAQ-1-MINUTE-MID-EXTERNAL"},
        {"bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL"},
        {"fast_ema_peroid": 2},
    ],
)
def test_common_runner_preserves_invalid_or_conflicting_identity_refusal(change):
    payload = _RunPayload(
        strategy_file=str(_STRATEGY_FILE),
        strategy_config={"fast_ema_period": 2, "slow_ema_period": 3} | change,
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path="unused",
    )
    with pytest.raises(ValueError):
        _build_strategy_config(payload)


@pytest.mark.parametrize(
    "provided",
    [
        {"instrument_id": "AAPL.NASDAQ"},
        {"bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"},
        {},
    ],
)
def test_common_runner_injects_only_absent_identity(provided):
    payload = _RunPayload(
        strategy_file=str(_STRATEGY_FILE),
        strategy_config=provided,
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path="unused",
    )
    native = _build_strategy_config(payload)
    assert native.config["instrument_id"] == "AAPL.NASDAQ"
    assert native.config["bar_type"] == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    assert payload.strategy_config == provided


@pytest.mark.parametrize("raw,expected", [("false", False), (0, False), ("true", True), (1, True)])
def test_common_runner_forwards_validated_boolean(raw, expected):
    from importlib import import_module

    payload = _RunPayload(
        strategy_file=str(_STRATEGY_FILE.parent / "smoke_market_order.py"),
        strategy_config={"manage_stop": raw},
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path="unused",
    )
    config = _build_strategy_config(payload)
    assert config.config["manage_stop"] is expected
    module, name = config.config_path.split(":")
    assert getattr(import_module(module), name)(**config.config).manage_stop is expected
    assert payload.strategy_config == {"manage_stop": raw}


def test_common_runner_preserves_native_defaults_and_decimal_identity():
    from importlib import import_module

    payload = _RunPayload(
        strategy_file=str(_STRATEGY_FILE.parent / "smoke_market_order.py"),
        strategy_config={},
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path="unused",
    )
    config = _build_strategy_config(payload)
    module, name = config.config_path.split(":")
    smoke = getattr(import_module(module), name)(**config.config)
    assert smoke.manage_stop is True
    assert smoke.order_id_tag is None
    payload.strategy_file = str(_STRATEGY_FILE)
    payload.strategy_config = {"trade_size": "1.2300"}
    config = _build_strategy_config(payload)
    assert config.config["trade_size"] == "1.2300"
    module, name = config.config_path.split(":")
    native = getattr(import_module(module), name)(**config.config)
    assert str(native.instrument_id) == "AAPL.NASDAQ"
    assert str(native.bar_type) == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
    assert str(native.trade_size) == "1.2300"
