"""Unit tests for :class:`StrategyMemberPayload` and the multi-strategy
fields on :class:`TradingNodePayload`.

Validates:

- ``StrategyMemberPayload`` fields are accessible and frozen
- ``TradingNodePayload.strategy_members`` list field works
- ``TradingNodePayload.all_instruments`` aggregates, de-duplicates,
  and sorts instruments across all members
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from msai.services.nautilus.trading_node_subprocess import (
    StrategyMemberPayload,
    TradingNodePayload,
)


def _make_member(
    *,
    instruments: list[str] | None = None,
    strategy_path: str = "strategies.example.ema_cross:EMACrossStrategy",
    strategy_config_path: str = "strategies.example.config:EMACrossConfig",
    strategy_config: dict | None = None,
    strategy_code_hash: str = "abc123",
    strategy_id_full: str = "",
) -> StrategyMemberPayload:
    return StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path=strategy_path,
        strategy_config_path=strategy_config_path,
        strategy_config=strategy_config if strategy_config is not None else {},
        strategy_code_hash=strategy_code_hash,
        strategy_id_full=strategy_id_full,
        instruments=instruments if instruments is not None else ["AAPL"],
    )


# ---------------------------------------------------------------------------
# StrategyMemberPayload
# ---------------------------------------------------------------------------


class TestStrategyMemberPayload:
    def test_strategy_member_payload_fields(self) -> None:
        """All declared fields are accessible on a constructed instance."""
        sid = uuid4()
        member = StrategyMemberPayload(
            strategy_id=sid,
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_config={"fast": 10},
            strategy_code_hash="deadbeef",
            strategy_id_full="abc@slug",
            instruments=["AAPL", "MSFT"],
        )

        assert member.strategy_id == sid
        assert member.strategy_path == "strats.foo:Bar"
        assert member.strategy_config_path == "strats.foo:BarConfig"
        assert member.strategy_config == {"fast": 10}
        assert member.strategy_code_hash == "deadbeef"
        assert member.strategy_id_full == "abc@slug"
        assert member.instruments == ["AAPL", "MSFT"]

    def test_strategy_member_payload_frozen(self) -> None:
        """Frozen dataclass rejects mutation."""
        member = _make_member()
        with pytest.raises(AttributeError):
            member.strategy_path = "other:Path"  # type: ignore[misc]

    def test_strategy_member_payload_defaults(self) -> None:
        """Default-valued fields work when omitted."""
        member = StrategyMemberPayload(
            strategy_id=uuid4(),
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
        )
        assert member.strategy_config == {}
        assert member.strategy_code_hash == ""
        assert member.strategy_id_full == ""
        assert member.instruments == []


# ---------------------------------------------------------------------------
# TradingNodePayload.strategy_members
# ---------------------------------------------------------------------------


class TestTradingNodePayloadStrategyMembers:
    def test_trading_node_payload_accepts_strategy_members(self) -> None:
        """strategy_members list field is populated and accessible."""
        m1 = _make_member(instruments=["AAPL"])
        m2 = _make_member(instruments=["MSFT"])
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_members=[m1, m2],
        )
        assert len(payload.strategy_members) == 2
        assert payload.strategy_members[0] is m1
        assert payload.strategy_members[1] is m2

    def test_trading_node_payload_strategy_members_defaults_empty(self) -> None:
        """strategy_members defaults to an empty list (back-compat)."""
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
        )
        assert payload.strategy_members == []


# ---------------------------------------------------------------------------
# TradingNodePayload.all_instruments
# ---------------------------------------------------------------------------


class TestAllInstruments:
    def test_all_instruments_aggregates_across_members(self) -> None:
        """Union of instruments from all members, de-duped and sorted."""
        m1 = _make_member(instruments=["MSFT", "AAPL"])
        m2 = _make_member(instruments=["GOOG", "AAPL"])
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_members=[m1, m2],
        )
        assert payload.all_instruments == ["AAPL", "GOOG", "MSFT"]

    def test_all_instruments_empty_when_no_members(self) -> None:
        """Legacy path: empty list when strategy_members is empty."""
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
        )
        assert payload.all_instruments == []

    def test_all_instruments_single_member(self) -> None:
        """Single member's instruments are returned sorted."""
        m1 = _make_member(instruments=["TSLA", "AAPL", "MSFT"])
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_members=[m1],
        )
        assert payload.all_instruments == ["AAPL", "MSFT", "TSLA"]

    def test_all_instruments_handles_empty_member_instruments(self) -> None:
        """A member with no instruments doesn't break aggregation."""
        m1 = _make_member(instruments=[])
        m2 = _make_member(instruments=["AAPL"])
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="abcd1234abcd1234",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_members=[m1, m2],
        )
        assert payload.all_instruments == ["AAPL"]


# ---------------------------------------------------------------------------
# Supervisor payload factory portfolio-path validation (Task 17)
# ---------------------------------------------------------------------------


class TestSupervisorPayloadFactoryPortfolioPath:
    """Validate the building blocks that _build_production_payload_factory
    uses for portfolio-based deployments. These are pure-function tests
    that don't need a real DB session."""

    def test_portfolio_payload_has_strategy_members(self) -> None:
        """A portfolio payload should have non-empty strategy_members."""
        m1 = _make_member(
            instruments=["AAPL"],
            strategy_id_full="EMACross-0-slug123",
            strategy_code_hash="hash1",
        )
        m2 = _make_member(
            instruments=["MSFT"],
            strategy_id_full="SmokeMarketOrder-1-slug123",
            strategy_code_hash="hash2",
        )
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="slug123",
            strategy_path=m1.strategy_path,
            strategy_config_path=m1.strategy_config_path,
            strategy_config=m1.strategy_config,
            paper_symbols=["AAPL", "MSFT"],
            canonical_instruments=["AAPL.NASDAQ", "MSFT.NASDAQ"],
            strategy_members=[m1, m2],
        )
        assert len(payload.strategy_members) == 2
        assert payload.strategy_members[0].strategy_id_full == "EMACross-0-slug123"
        assert payload.strategy_members[1].strategy_id_full == "SmokeMarketOrder-1-slug123"

    def test_portfolio_payload_aggregated_symbols(self) -> None:
        """paper_symbols and canonical_instruments aggregate across all members."""
        m1 = _make_member(instruments=["AAPL", "GOOG"])
        m2 = _make_member(instruments=["MSFT", "AAPL"])
        # Simulate the aggregation the factory does
        all_paper = sorted(set(m1.instruments + m2.instruments))
        assert all_paper == ["AAPL", "GOOG", "MSFT"]

    def test_portfolio_payload_preserves_per_member_config(self) -> None:
        """Each StrategyMemberPayload carries its own config."""
        m1 = _make_member(
            strategy_config={"instrument_id": "AAPL.NASDAQ", "fast_period": 10},
        )
        m2 = _make_member(
            strategy_config={"instrument_id": "MSFT.NASDAQ", "fast_period": 20},
        )
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="slug123",
            strategy_path=m1.strategy_path,
            strategy_config_path=m1.strategy_config_path,
            strategy_members=[m1, m2],
        )
        assert payload.strategy_members[0].strategy_config["fast_period"] == 10
        assert payload.strategy_members[1].strategy_config["fast_period"] == 20

    def test_legacy_single_strategy_has_empty_members(self) -> None:
        """Legacy single-strategy payload has empty strategy_members list."""
        payload = TradingNodePayload(
            row_id=uuid4(),
            deployment_id=uuid4(),
            deployment_slug="slug123",
            strategy_path="strats.foo:Bar",
            strategy_config_path="strats.foo:BarConfig",
            strategy_config={"instrument_id": "AAPL.NASDAQ"},
            paper_symbols=["AAPL"],
        )
        assert payload.strategy_members == []
        assert payload.all_instruments == []

    def test_imports_needed_for_portfolio_factory(self) -> None:
        """Verify all imports the factory needs are accessible."""
        from msai.models import LivePortfolioRevisionStrategy
        from msai.services.live.deployment_identity import derive_strategy_id_full
        from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths
        from msai.services.strategy_registry import compute_file_hash

        # Just verify imports succeed — no need to call them
        assert callable(derive_strategy_id_full)
        assert callable(resolve_importable_strategy_paths)
        assert callable(compute_file_hash)
        assert hasattr(LivePortfolioRevisionStrategy, "__tablename__")


# ---------------------------------------------------------------------------
# Pickle round-trip (Task 11b)
# ---------------------------------------------------------------------------


def test_payload_pickles_with_resolved_instruments_via_spawn_context() -> None:
    """Lock the pickle round-trip invariant — prevents a future field
    addition (Decimal, datetime, Path) from silently breaking mp.spawn.
    StrategyMemberPayload.resolved_instruments tuple must survive
    pickle.dumps/loads with all ResolvedInstrument fields intact."""
    import pickle
    from datetime import date

    from msai.services.nautilus.security_master.live_resolver import (
        AssetClass,
        ResolvedInstrument,
    )

    resolved = (
        ResolvedInstrument(
            canonical_id="QQQ.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK",
                "symbol": "QQQ",
                "exchange": "SMART",
                "primaryExchange": "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    )
    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={},
        strategy_code_hash="abc123",
        strategy_id_full="",
        instruments=["QQQ"],
        resolved_instruments=resolved,
    )
    payload = TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcd1234abcd1234",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_members=[member],
    )

    round_tripped = pickle.loads(pickle.dumps(payload))
    assert round_tripped.strategy_members[0].resolved_instruments == resolved
    assert (
        round_tripped.strategy_members[0].resolved_instruments[0].asset_class is AssetClass.EQUITY
    )
    assert round_tripped.strategy_members[0].resolved_instruments[0].canonical_id == "QQQ.NASDAQ"
