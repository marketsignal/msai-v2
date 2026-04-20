"""Unit tests for Task 11 — build_portfolio_trading_node_config
aggregates member.resolved_instruments dedup'd by canonical_id."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_portfolio_trading_node_config,
)
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
)
from msai.services.nautilus.trading_node_subprocess import (
    StrategyMemberPayload,
)


def _make_resolved(
    canonical_id: str,
    ac: AssetClass = AssetClass.EQUITY,
) -> ResolvedInstrument:
    symbol = canonical_id.partition(".")[0]
    venue = canonical_id.partition(".")[2] or "NASDAQ"
    spec = {
        "secType": "STK",
        "symbol": symbol,
        "exchange": "SMART",
        "primaryExchange": venue,
        "currency": "USD",
    }
    return ResolvedInstrument(
        canonical_id=canonical_id,
        asset_class=ac,
        contract_spec=spec,
        effective_window=(date(2026, 1, 1), None),
    )


def _make_member(
    *,
    resolved_instruments: tuple[ResolvedInstrument, ...],
) -> StrategyMemberPayload:
    return StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.buy_hold:BuyHold",
        strategy_config_path="strategies.example.buy_hold:BuyHoldConfig",
        strategy_config={},
        strategy_code_hash="abc123",
        strategy_id_full="bh-001",
        instruments=[r.canonical_id.partition(".")[0] for r in resolved_instruments],
        resolved_instruments=resolved_instruments,
    )


def _ib_settings() -> IBSettings:
    return IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567")


def test_config_aggregates_resolved_instruments_across_members() -> None:
    """Two members with different resolved_instruments → two distinct
    IBContracts in load_contracts."""
    m1 = _make_member(resolved_instruments=(_make_resolved("AAPL.NASDAQ"),))
    m2 = _make_member(resolved_instruments=(_make_resolved("MSFT.NASDAQ"),))
    cfg = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[m1, m2],
        ib_settings=_ib_settings(),
    )
    load_contracts = cfg.data_clients["INTERACTIVE_BROKERS"].instrument_provider.load_contracts
    symbols = {c.symbol for c in load_contracts}
    assert symbols == {"AAPL", "MSFT"}


def test_config_dedups_same_canonical_across_members() -> None:
    """Both members subscribe AAPL → exactly one IBContract."""
    ri = _make_resolved("AAPL.NASDAQ")
    m1 = _make_member(resolved_instruments=(ri,))
    m2 = _make_member(resolved_instruments=(ri,))
    cfg = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[m1, m2],
        ib_settings=_ib_settings(),
    )
    load_contracts = cfg.data_clients["INTERACTIVE_BROKERS"].instrument_provider.load_contracts
    assert len(load_contracts) == 1


def test_config_raises_when_all_members_have_empty_resolved_instruments() -> None:
    """If supervisor didn't thread resolved_instruments (bug), fail
    fast at config build rather than silently preloading zero IB
    contracts. Members MUST have non-empty instruments (so the old
    check passes) but empty resolved_instruments (triggering new check)."""
    m = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.buy_hold:BuyHold",
        strategy_config_path="strategies.example.buy_hold:BuyHoldConfig",
        strategy_config={},
        strategy_code_hash="abc123",
        strategy_id_full="bh-001",
        instruments=["AAPL"],
        resolved_instruments=(),  # empty — supervisor bug
    )
    with pytest.raises(ValueError, match="resolved_instruments"):
        build_portfolio_trading_node_config(
            deployment_slug="abcd1234abcd1234",
            strategy_members=[m],
            ib_settings=_ib_settings(),
        )
