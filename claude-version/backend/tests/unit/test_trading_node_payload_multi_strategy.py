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
