"""Independent economics controls for the research-foundation repair."""

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from msai.services.analytics_math import build_series_payload, compute_series_metrics
from msai.services.nautilus import backtest_runner as runner
from msai.workers import backtest_job as worker


def _account(rows: list[tuple[str, str, float]], currency: str = "USD") -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["timestamp", "account_id", "total"]).assign(
        currency=currency
    )


@pytest.mark.parametrize("ending,expected", [(110.0, 0.1), (90.0, -0.1), (100.0, 0.0)])
def test_first_session_is_measured_against_known_opening_balance(ending, expected):
    report = _account([("2025-01-02T09:00Z", "A", 100.0), ("2025-01-02T16:00Z", "A", ending)])
    daily = runner._compact_account_report(report, opening_balances={"A": 100.0})
    assert daily["returns"].tolist() == pytest.approx([expected])
    assert daily["equity"].tolist() == [ending]


def test_asynchronous_accounts_keep_the_other_accounts_last_balance():
    report = _account(
        [
            ("2025-01-02T09:00Z", "A", 100.0),
            ("2025-01-02T09:01Z", "B", 100.0),
            ("2025-01-02T10:00Z", "A", 101.0),
            ("2025-01-02T11:00Z", "B", 102.0),
            ("2025-01-03T11:00Z", "B", 104.0),
        ]
    )
    daily = runner._compact_account_report(report, opening_balances={"A": 100, "B": 100})
    assert daily["equity"].tolist() == [203.0, 205.0]
    assert daily["returns"].tolist() == pytest.approx([0.015, 2 / 203])
    payload = build_series_payload(pd.Series(daily.returns.values, index=daily.timestamp), 200)
    assert payload["daily"][-1]["equity"] == pytest.approx(float(Decimal("205")))
    assert payload["monthly_returns"][0]["pct"] == pytest.approx(0.025)


@pytest.mark.parametrize("change", ["currency", "missing_currency", "opening", "nan"])
def test_ambiguous_account_economics_fail_explicitly(change):
    report = _account([("2025-01-02T16:00Z", "A", 110)])
    opening = {"A": 100}
    if change == "currency":
        report["currency"] = "EUR"
    elif change == "missing_currency":
        report = report.drop(columns="currency")
    elif change == "opening":
        opening = {"B": 100}
    else:
        report["total"] = float("nan")
    with pytest.raises(ValueError):
        runner._compact_account_report(report, opening_balances=opening)


def test_account_activity_gap_does_not_invent_exchange_sessions():
    report = _account([("2025-01-02T16:00Z", "A", 90), ("2025-01-06T16:00Z", "A", 90)])
    daily = runner._compact_account_report(report, opening_balances={"A": 100})
    assert daily["returns"].tolist() == pytest.approx([-0.1, 0])
    assert len(daily) == 2  # observed balance dates, not a certified exchange calendar


def test_initial_loss_matches_quantstats_report_drawdown():
    import quantstats as qs

    returns = pd.Series([-0.1, 0.0], index=pd.date_range("2025-01-02", periods=2, tz="UTC"))
    payload = build_series_payload(returns, 100)
    assert payload["daily"][0]["drawdown"] == pytest.approx(-0.1)
    assert compute_series_metrics(returns).max_drawdown == pytest.approx(-0.1)
    assert qs.stats.to_drawdown_series(returns).tolist() == pytest.approx([-0.1, -0.1])


@pytest.mark.parametrize(
    "key,value,expected", [("PnL% (total)", 10, 0.1), ("return", 0.1, 0.1), ("PnL% (total)", 0, 0)]
)
def test_native_percent_is_converted_but_ratio_and_real_zero_are_preserved(key, value, expected):
    primary = SimpleNamespace(stats_returns={}, stats_pnls={"USD": {key: value, "Win Rate": 0}})
    positions = pd.DataFrame({"side": ["FLAT"], "realized_pnl": ["100 USD"]})
    result = runner._extract_metrics(primary, pd.DataFrame(), positions_df=positions)
    assert result["total_return"] == pytest.approx(expected)
    assert result["win_rate"] == 0


def test_daily_account_series_owns_headline_metrics_even_when_native_stats_are_nonzero():
    primary = SimpleNamespace(
        stats_returns={"Sharpe Ratio": 100, "Sortino Ratio": 100, "Max Drawdown": -0.99},
        stats_pnls={"USD": {"PnL% (total)": 200}},
    )
    account = pd.DataFrame(
        {"timestamp": pd.date_range("2025-01-02", periods=2, tz="UTC"), "returns": [-0.1, 0]}
    )
    metrics = runner._extract_metrics(primary, pd.DataFrame(), account)
    assert metrics["total_return"] == pytest.approx(-0.1)
    assert metrics["max_drawdown"] == pytest.approx(-0.1)
    assert metrics["sharpe_ratio"] == pytest.approx(-(252**0.5))
    assert metrics["sortino_ratio"] == pytest.approx(-(126**0.5))


def _native_reports():
    from nautilus_trader.analysis.reporter import ReportProvider
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.identifiers import ClientOrderId, TradeId
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs

    instrument = TestInstrumentProvider.equity()
    order = TestExecStubs.limit_order(instrument=instrument, quantity=Quantity.from_int(10))
    order.apply(TestEventStubs.order_accepted(order))
    for identity, qty, px, fee, timestamp in [
        ("F1", 2, "100.00", 1.2, 1),
        ("F2", 3, "101.00", 0.0, 2),
    ]:
        order.apply(
            TestEventStubs.order_filled(
                order,
                instrument,
                trade_id=TradeId(identity),
                last_qty=Quantity.from_int(qty),
                last_px=Price.from_str(px),
                commission=Money(fee, USD),
                ts_event=timestamp * 1_000_000_000,
            )
        )
    canceled = TestExecStubs.limit_order(
        instrument=instrument,
        client_order_id=ClientOrderId("CANCELED"),
        quantity=Quantity.from_int(10),
    )
    canceled.apply(TestEventStubs.order_accepted(canceled))
    canceled.apply(TestEventStubs.order_canceled(canceled))
    return (
        ReportProvider.generate_orders_report([order, canceled]),
        ReportProvider.generate_fills_report([order, canceled]).reset_index(),
    )


def test_actual_partial_fills_persist_their_economics_and_identity():
    orders, fills = _native_reports()
    trades = [
        worker._fill_row_to_trade(
            fill=row, backtest_id="backtest", strategy_id="strategy", strategy_code_hash="a" * 64
        )
        for row in fills.to_dict(orient="records")
    ]
    assert orders["quantity"].astype(float).sum() == 20  # includes canceled intent
    assert [trade.quantity for trade in trades] == [Decimal("2"), Decimal("3")]
    assert [trade.price for trade in trades] == [Decimal("100"), Decimal("101")]
    assert [trade.commission for trade in trades] == [Decimal("1.20"), Decimal("0")]
    assert [trade.pnl for trade in trades] == [None, None]
    assert [trade.broker_trade_id for trade in trades] == ["F1", "F2"]
    assert trades[0].client_order_id == trades[1].client_order_id
    assert trades[0].executed_at < trades[1].executed_at


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_qty", "10broken"),
        ("last_qty", "0"),
        ("last_px", "nan"),
        ("currency", "EUR"),
        ("commission", "2 EUR"),
        ("commission", "broken"),
        ("ts_event", None),
    ],
)
def test_malformed_or_unsupported_fill_economics_are_rejected(field, value):
    _, fills = _native_reports()
    fill = fills.to_dict(orient="records")[0]
    fill[field] = value
    with pytest.raises(ValueError):
        worker._fill_row_to_trade(
            fill=fill, backtest_id="b", strategy_id="s", strategy_code_hash="a"
        )


def test_fill_count_is_the_research_eligibility_input():
    from msai.services.research_engine import is_stage_eligible

    _, fills = _native_reports()
    metrics = runner._extract_metrics(SimpleNamespace(), fills)
    assert metrics["num_trades"] == metrics["num_fills"] == 2
    assert not is_stage_eligible(
        result={"metrics": metrics}, stage_fraction=1, min_trades=3, require_positive_return=False
    )


def test_series_uses_engine_capital_and_retains_accounting_scope():
    accounting = {
        "version": 1,
        "basis": "realized_account_balance",
        "initial_capital": 1_000_000,
        "currency": "USD",
        "costs": "engine_recorded",
    }
    returns = pd.Series([-0.1], index=pd.date_range("2025-01-02", periods=1, tz="UTC"))
    payload, status = worker._materialize_series_payload(returns, "b", accounting=accounting)
    assert status == "ready"
    assert payload["accounting"] == accounting
    assert payload["daily"][0]["equity"] == pytest.approx(900_000)


def test_portfolio_counts_fills_while_parity_keeps_requested_order_intent(monkeypatch):
    from msai.services.nautilus.parity.normalizer import normalize_orders_df
    from msai.services.portfolio import orchestration

    orders, fills = _native_reports()
    # A single partly filled intent and a canceled intent have two order rows,
    # but the two executions belong solely to the first order.
    intents = normalize_orders_df(orders)
    assert [item.signed_qty for item in intents] == [Decimal("10"), Decimal("10")]
    account = pd.DataFrame(
        {"timestamp": pd.date_range("2025-01-02", periods=1, tz="UTC"), "returns": [0.0]}
    )
    result = runner.BacktestResult(orders, pd.DataFrame(), account, {}, fills_df=fills.iloc[:1])
    monkeypatch.setattr(orchestration, "ensure_catalog_data", lambda **kwargs: ["AAPL.XNAS"])
    monkeypatch.setattr(orchestration, "resolve_strategy_file", lambda path: path)
    outcome = orchestration.PortfolioService()._run_candidate_backtest(
        SimpleNamespace(run=lambda **kwargs: result),
        {
            "candidate_id": "candidate",
            "strategy_id": "strategy",
            "strategy_name": "example",
            "strategy_file_path": "example.py",
            "instruments": ["AAPL"],
            "weight": 1,
            "config": {},
        },
        "2025-01-02",
        "2025-01-03",
    )
    assert outcome["trade_count"] == 1


def test_report_notice_includes_actual_capital_and_scope_without_html_injection():
    accounting = {"initial_capital": 1_000_000, "currency": "USD<script>"}
    report = worker._add_accounting_notice(
        '<html><body class="report">result</body></html>', accounting
    )
    assert "1,000,000.00 USD&lt;script&gt;" in report
    assert "excludes unrealized gains and losses" in report
    assert "Fees are engine-recorded" in report
    assert "insufficient observations" in report
    assert "<script>" not in report


@pytest.mark.parametrize("first,drawdown", [(-0.1, -0.1), (0.1, -0.005)])
def test_actual_quantstats_html_has_canonical_headlines_for_first_loss_and_gain(first, drawdown):
    import re

    from msai.services.report_generator import ReportGenerator

    returns = pd.Series(
        [first, 0, 0.01, -0.005, 0, 0.001, 0.002, 0, -0.001, 0],
        index=pd.date_range("2025-01-02", periods=10, tz="UTC"),
    )
    html = ReportGenerator().generate_tearsheet(returns)
    metrics = compute_series_metrics(returns)
    report = worker._add_accounting_notice(
        html,
        {"initial_capital": 100, "currency": "USD"},
        metrics={
            "total_return": metrics.total_return,
            "max_drawdown": metrics.max_drawdown,
            "sharpe_ratio": metrics.sharpe,
            "sortino_ratio": metrics.sortino,
        },
    )
    assert metrics.max_drawdown == pytest.approx(drawdown)
    cells = re.findall(r"<tr><td>Max Drawdown</td><td>([^<]*)</td></tr>", report)
    assert cells == [f"{drawdown:.2%}"]
    assert "Supplemental QuantStats statistics" in report


def test_nonmatching_quantstats_markup_uses_canonical_report_instead_of_wrong_cells():
    html = "<html><body><tr><td>Max Drawdown</td><td><span>wrong</span></td></tr></body></html>"
    report = worker._add_accounting_notice(
        html,
        {"initial_capital": 100, "currency": "USD"},
        metrics={"total_return": -0.1, "max_drawdown": -0.1, "sharpe_ratio": 0, "sortino_ratio": 0},
    )
    assert "wrong" not in report
    assert "-10.00%" in report
    assert "Supplemental report unavailable" in report


def test_report_keeps_small_nonzero_return_and_drawdown_visible():
    report = worker._add_accounting_notice(
        "<html><body><tr><td>Cumulative Return</td><td>-0.00%</td></tr>"
        "<tr><td>Max Drawdown</td><td>-0.00%</td></tr></body></html>",
        {"initial_capital": 1_000_000, "currency": "USD"},
        metrics={
            "total_return": -0.00000053,
            "max_drawdown": -0.00000087,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
        },
    )
    assert "-0.0000530%" in report
    assert "-0.0000870%" in report
    assert "-0.00%" not in report


@pytest.mark.asyncio
async def test_finalizer_preserves_metadata_and_individual_fills_when_series_fails(monkeypatch):
    from contextlib import asynccontextmanager
    from uuid import uuid4

    row = SimpleNamespace(id=uuid4())
    added = []

    class Session:
        async def get(self, model, identity):
            return row

        def add(self, value):
            added.append(value)

        async def commit(self):
            pass

    @asynccontextmanager
    async def factory():
        yield Session()

    monkeypatch.setattr(worker, "async_session_factory", factory)
    _, fills = _native_reports()
    accounting = {
        "version": 1,
        "basis": "realized_account_balance",
        "initial_capital": 1_000_000,
        "currency": "USD",
        "costs": "engine_recorded",
    }
    await worker._finalize_backtest(
        backtest_id=str(row.id),
        metrics={"num_trades": 2, "num_fills": 2},
        report_path="report.html",
        fills_df=fills,
        strategy_id=uuid4(),
        strategy_code_hash="a" * 64,
        series_payload=None,
        series_status="failed",
        accounting=accounting,
    )
    assert row.metrics["accounting"] == accounting
    assert row.series_status == "failed"
    assert len(added) == 2
    assert sum(trade.quantity for trade in added) == Decimal("5")
    assert sum(trade.commission for trade in added) == Decimal("1.20")
    assert all(trade.pnl is None for trade in added)
