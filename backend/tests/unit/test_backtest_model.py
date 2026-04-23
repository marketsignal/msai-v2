"""Tests for the Backtest model's new error classification columns."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from msai.models.backtest import Backtest
from msai.models.trade import Trade
from tests.unit.conftest import (
    _make_backtest,
    _make_backtest_completed_with_series,
    _make_backtest_failed_series,
    _make_backtest_legacy,
    _make_backtest_with_trades,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_backtest_has_error_code_column():
    assert hasattr(Backtest, "error_code")
    assert Backtest.__table__.c.error_code.type.length == 32
    assert not Backtest.__table__.c.error_code.nullable
    assert Backtest.__table__.c.error_code.server_default.arg == "unknown"


def test_backtest_has_error_public_message_column():
    assert hasattr(Backtest, "error_public_message")
    assert Backtest.__table__.c.error_public_message.nullable is True


def test_backtest_has_error_suggested_action_column():
    assert hasattr(Backtest, "error_suggested_action")
    assert Backtest.__table__.c.error_suggested_action.nullable is True


def test_backtest_has_error_remediation_column():
    assert hasattr(Backtest, "error_remediation")
    # JSONB subtype
    assert Backtest.__table__.c.error_remediation.nullable is True
    assert "JSONB" in str(Backtest.__table__.c.error_remediation.type)


# ---------------------------------------------------------------------------
# series + series_status columns
# ---------------------------------------------------------------------------


def test_backtest_model_has_series_attribute() -> None:
    assert hasattr(Backtest, "series")
    assert hasattr(Backtest, "series_status")


def test_backtest_model_defaults() -> None:
    """Unset series_status defaults to 'not_materialized' at DB level."""
    bt = _make_backtest()
    # Value only populated after flush/insert; the DB DEFAULT handles new rows.
    assert bt.series is None
    # No Python-side default; DB DEFAULT handles it.


def test_backtest_series_column_is_nullable_jsonb() -> None:
    col = Backtest.__table__.c.series
    assert col.nullable is True
    assert "JSONB" in str(col.type)


def test_backtest_series_status_column_is_non_null_varchar32_with_default() -> None:
    col = Backtest.__table__.c.series_status
    assert col.nullable is False
    assert col.type.length == 32
    # server_default is wrapped in TextClause; compare the rendered SQL.
    assert "not_materialized" in str(col.server_default.arg)


# ---------------------------------------------------------------------------
# Pure-factory helpers smoke test
# ---------------------------------------------------------------------------
#
# Confirms the new helpers in ``conftest.py`` produce valid in-memory objects
# with the expected series_status/payload shape. No DB round-trip; these are
# consumed by mocked-session tests.


def test_make_backtest_completed_with_series_defaults() -> None:
    bt = _make_backtest_completed_with_series()
    assert isinstance(bt, Backtest)
    assert bt.status == "completed"
    assert bt.series_status == "ready"
    assert bt.series is not None
    assert "daily" in bt.series
    assert "monthly_returns" in bt.series
    assert len(bt.series["daily"]) == 2
    assert bt.series["daily"][0]["date"] == "2024-01-02"
    assert bt.metrics == {"sharpe_ratio": 2.1, "total_return": 0.01, "num_trades": 4}
    assert bt.report_path == "/tmp/ready-report.html"


def test_make_backtest_completed_with_series_allows_overrides() -> None:
    custom_series: dict[str, list[object]] = {"daily": [], "monthly_returns": []}
    bt = _make_backtest_completed_with_series(series=custom_series, series_status="ready")
    assert bt.series == custom_series
    assert bt.series_status == "ready"


def test_make_backtest_legacy_has_not_materialized_status() -> None:
    bt = _make_backtest_legacy()
    assert bt.status == "completed"
    assert bt.series is None
    # CRITICAL: explicit-set in factory, not relying on server_default
    assert bt.series_status == "not_materialized"
    assert bt.metrics == {"sharpe_ratio": 1.2, "total_return": 0.05, "num_trades": 10}


def test_make_backtest_failed_series_has_failed_status() -> None:
    bt = _make_backtest_failed_series()
    assert bt.status == "completed"
    assert bt.series is None
    assert bt.series_status == "failed"
    assert bt.metrics == {"sharpe_ratio": 0.8, "total_return": 0.02, "num_trades": 6}


def test_make_backtest_with_trades_returns_tuple_with_n_trades() -> None:
    bt, trades = _make_backtest_with_trades(5)
    assert isinstance(bt, Backtest)
    assert len(trades) == 5
    for t in trades:
        assert isinstance(t, Trade)
        assert t.backtest_id == bt.id
        assert t.strategy_id == bt.strategy_id
        assert t.strategy_code_hash == bt.strategy_code_hash
        assert t.instrument == "SPY.XNAS"
        assert t.quantity == Decimal("10")
        assert t.price == Decimal("450.00")
        assert t.commission == Decimal("0.50")


def test_make_backtest_with_trades_pnl_none_every_third() -> None:
    """Every third trade has pnl=None to exercise the coalesce path."""
    _, trades = _make_backtest_with_trades(9)
    # i=0, 3, 6 have pnl=None; others have Decimal("5.00")
    assert trades[0].pnl is None
    assert trades[1].pnl == Decimal("5.00")
    assert trades[2].pnl == Decimal("5.00")
    assert trades[3].pnl is None
    assert trades[4].pnl == Decimal("5.00")
    assert trades[5].pnl == Decimal("5.00")
    assert trades[6].pnl is None


def test_make_backtest_with_trades_sides_alternate() -> None:
    _, trades = _make_backtest_with_trades(4)
    assert trades[0].side == "BUY"
    assert trades[1].side == "SELL"
    assert trades[2].side == "BUY"
    assert trades[3].side == "SELL"


def test_account_df_factory_produces_tz_aware_returns(
    account_df_factory: Callable[..., pd.DataFrame],
) -> None:
    """Fixture yields a factory callable; defaults to 21 business days."""
    df = account_df_factory()
    assert isinstance(df, pd.DataFrame)
    assert "returns" in df.columns
    assert len(df) == 21
    # Index must be tz-aware (UTC) so Nautilus report-parsing paths line up.
    assert df.index.tz is not None
    assert str(df.index.tz) == "UTC"


def test_account_df_factory_honors_periods_kwarg(
    account_df_factory: Callable[..., pd.DataFrame],
) -> None:
    df = account_df_factory(periods=5)
    assert len(df) == 5
