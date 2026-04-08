"""Unit tests for PositionReader (Phase 3 task 3.5).

We do NOT touch a real Redis or a real Nautilus Cache here —
those are exercised by the integration test in
``tests/integration/test_position_reader.py``. The unit tests
focus on the fast-path / cold-path branching logic and the
hydration interaction with ProjectionState.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    FillEvent,
    PositionSnapshot,
)
from msai.services.nautilus.projection.position_reader import PositionReader
from msai.services.nautilus.projection.projection_state import ProjectionState

NOW = datetime.now(UTC)


def _build_reader() -> tuple[PositionReader, ProjectionState]:
    state = ProjectionState()
    reader = PositionReader(projection_state=state)
    return reader, state


def _snapshot(deployment_id, instrument_id="AAPL.NASDAQ", qty="100") -> PositionSnapshot:
    return PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id=instrument_id,
        qty=Decimal(qty),
        avg_price=Decimal("150"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=NOW,
    )


# ----------------------------------------------------------------------
# Fast path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_positions_fast_path_returns_state() -> None:
    reader, state = _build_reader()
    deployment_id = uuid4()
    state.apply(_snapshot(deployment_id, "AAPL.NASDAQ"))

    with patch.object(reader, "_read_via_ephemeral_cache_positions") as mock_cold:
        result = await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    assert len(result) == 1
    assert result[0].instrument_id == "AAPL.NASDAQ"
    # Cold path must NOT have been called
    mock_cold.assert_not_called()


@pytest.mark.asyncio
async def test_get_open_positions_fast_path_empty_hydrated_returns_empty_without_cold_read() -> (
    None
):
    """v7 regression test for Codex v6 P1: an empty-but-hydrated
    deployment must return [] WITHOUT triggering the cold path.
    A previous design used `if positions:` as the fast-path
    check, so an idle deployment fell through to Redis on every
    request."""
    reader, state = _build_reader()
    deployment_id = uuid4()
    state.hydrate_from_cold_read(deployment_id, positions=[])

    assert state.is_positions_hydrated(deployment_id) is True

    with patch.object(reader, "_read_via_ephemeral_cache_positions") as mock_cold:
        result = await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    assert result == []
    mock_cold.assert_not_called()


@pytest.mark.asyncio
async def test_fill_event_does_not_fake_hydrate() -> None:
    """v7 regression test for Codex v6 P1: a FillEvent must NOT
    flip the positions-hydrated flag. If it did, the fast path
    would return [] when real positions exist in Redis."""
    reader, state = _build_reader()
    deployment_id = uuid4()
    fill = FillEvent(
        deployment_id=deployment_id,
        client_order_id="ord-1",
        instrument_id="AAPL.NASDAQ",
        side="BUY",
        qty=Decimal("10"),
        price=Decimal("150"),
        commission=Decimal("0"),
        ts=NOW,
    )
    state.apply(fill)

    assert state.is_positions_hydrated(deployment_id) is False

    with patch.object(
        reader,
        "_read_via_ephemeral_cache_positions",
        return_value=[_snapshot(deployment_id, "AAPL.NASDAQ", "10")],
    ) as mock_cold:
        await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    # Cold path WAS called because the FillEvent didn't flip the flag
    mock_cold.assert_called_once()


# ----------------------------------------------------------------------
# Cold path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_path_hydrates_state_after_first_read() -> None:
    reader, state = _build_reader()
    deployment_id = uuid4()
    cold_result = [_snapshot(deployment_id, "AAPL.NASDAQ", "100")]

    with patch.object(
        reader, "_read_via_ephemeral_cache_positions", return_value=cold_result
    ) as mock_cold:
        result = await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    assert len(result) == 1
    assert state.is_positions_hydrated(deployment_id) is True
    mock_cold.assert_called_once()


@pytest.mark.asyncio
async def test_cold_path_fires_only_once() -> None:
    """v7: after the first cold read, subsequent reads MUST hit
    the fast path. Mock the cold path to raise on second call —
    if the fast path is wired correctly, the second call never
    reaches the cold path."""
    reader, state = _build_reader()
    deployment_id = uuid4()
    cold_result = [_snapshot(deployment_id, "AAPL.NASDAQ", "100")]

    call_count = 0

    def cold_side_effect(*args: Any, **kwargs: Any) -> list[PositionSnapshot]:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise RuntimeError("cold path called more than once")
        return cold_result

    with patch.object(
        reader,
        "_read_via_ephemeral_cache_positions",
        side_effect=cold_side_effect,
    ):
        await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )
        # Second call must NOT touch the cold path
        result = await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    assert len(result) == 1
    assert call_count == 1


@pytest.mark.asyncio
async def test_cold_path_only_if_still_cold_race_returns_fresher_data() -> None:
    """v8 regression test for Codex v7 P1: the hydrate is a
    no-op if the StateApplier raced us. The reader returns the
    CURRENT state value, not the cold-read result, so the
    fresher pub/sub data wins the race."""
    reader, state = _build_reader()
    deployment_id = uuid4()

    # Simulate StateApplier writing fresher data WHILE the cold
    # read is in flight: the cold-read function below sees an
    # empty state, but returns a stale snapshot. We pre-populate
    # state RIGHT BEFORE the hydrate runs by patching the cold
    # path to do it as a side effect.
    fresher = _snapshot(deployment_id, "AAPL.NASDAQ", "200")
    stale = [_snapshot(deployment_id, "AAPL.NASDAQ", "100")]

    def cold_side_effect(*args: Any, **kwargs: Any) -> list[PositionSnapshot]:
        # Race the StateApplier: it apply()s the fresher snapshot
        # between our fast-path check and our hydrate call.
        state.apply(fresher)
        return stale

    with patch.object(
        reader,
        "_read_via_ephemeral_cache_positions",
        side_effect=cold_side_effect,
    ):
        result = await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )

    # Result must be the fresher (qty=200) snapshot, not the
    # stale cold-read (qty=100)
    assert len(result) == 1
    assert result[0].qty == Decimal("200")


# ----------------------------------------------------------------------
# Account
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_account_fast_path_when_hydrated() -> None:
    reader, state = _build_reader()
    deployment_id = uuid4()
    account = AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("100000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        ts=NOW,
    )
    state.apply(account)

    with patch.object(reader, "_read_via_ephemeral_cache_account") as mock_cold:
        result = await reader.get_account(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            account_id="DU12345",
        )

    assert result is not None
    assert result.balance == Decimal("100000")
    mock_cold.assert_not_called()


@pytest.mark.asyncio
async def test_get_account_cold_path_hydrates_state() -> None:
    reader, state = _build_reader()
    deployment_id = uuid4()
    cold_account = AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("50000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("50000"),
        ts=NOW,
    )

    with patch.object(
        reader, "_read_via_ephemeral_cache_account", return_value=cold_account
    ) as mock_cold:
        result = await reader.get_account(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            account_id="DU12345",
        )

    assert result is not None
    assert result.balance == Decimal("50000")
    assert state.is_account_hydrated(deployment_id) is True
    mock_cold.assert_called_once()


# ----------------------------------------------------------------------
# Per-domain hydration isolation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_account_cold_read_returns_none_hydrates_none() -> None:
    """Codex batch 8 P1 regression: when the cold read returns
    None ("the cold read found no account"), the reader MUST
    still hydrate the state so the next call serves None from
    the fast path. Without this fix, every call for an
    account-less deployment cold-reads Redis."""
    reader, state = _build_reader()
    deployment_id = uuid4()

    call_count = 0

    def cold_side_effect(*args: Any, **kwargs: Any) -> AccountStateUpdate | None:
        nonlocal call_count
        call_count += 1
        return None

    with patch.object(
        reader,
        "_read_via_ephemeral_cache_account",
        side_effect=cold_side_effect,
    ):
        first = await reader.get_account(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            account_id="DU12345",
        )
        second = await reader.get_account(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            account_id="DU12345",
        )

    assert first is None
    assert second is None
    # Cold path fired exactly once across both calls
    assert call_count == 1
    assert state.is_account_hydrated(deployment_id) is True


@pytest.mark.asyncio
async def test_per_domain_hydration_positions_only() -> None:
    """v7: hydrating positions must NOT mark account as hydrated
    and vice versa."""
    reader, state = _build_reader()
    deployment_id = uuid4()
    state.hydrate_from_cold_read(
        deployment_id,
        positions=[_snapshot(deployment_id, "AAPL.NASDAQ", "100")],
    )

    assert state.is_positions_hydrated(deployment_id) is True
    assert state.is_account_hydrated(deployment_id) is False

    # get_open_positions takes the fast path
    with patch.object(reader, "_read_via_ephemeral_cache_positions") as mock_pos_cold:
        await reader.get_open_positions(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            strategy_id_full="EMACross-test",
        )
    mock_pos_cold.assert_not_called()

    # get_account takes the cold path
    cold_account = AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("100000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        ts=NOW,
    )
    with patch.object(
        reader, "_read_via_ephemeral_cache_account", return_value=cold_account
    ) as mock_acct_cold:
        await reader.get_account(
            deployment_id=deployment_id,
            trader_id="MSAI-test",
            account_id="DU12345",
        )
    mock_acct_cold.assert_called_once()


# ----------------------------------------------------------------------
# Adapter construction (regression for Codex v4 P1 + v5 P1)
# ----------------------------------------------------------------------


def test_build_adapter_constructor_signature() -> None:
    """Regression test for Codex v4 P1: ensure
    ``CacheDatabaseAdapter`` is constructed with all four
    required arguments (trader_id, instance_id, serializer,
    config). A missing arg would raise TypeError on
    construction.

    The Cython class is immutable so we can't monkey-patch
    its ``__init__``. Instead we patch the symbol where
    PositionReader imports it (the standard "patch where
    used" pattern) — replacing the class with a stand-in
    that records construction kwargs.
    """
    reader, _ = _build_reader()
    construction_kwargs: dict[str, Any] = {}

    class StandIn:
        def __init__(self, **kwargs: Any) -> None:
            construction_kwargs.update(kwargs)

    with patch(
        "msai.services.nautilus.projection.position_reader.CacheDatabaseAdapter",
        StandIn,
    ):
        reader._build_adapter("MSAI-test")  # noqa: SLF001

    assert "trader_id" in construction_kwargs
    assert "instance_id" in construction_kwargs
    assert "serializer" in construction_kwargs
    assert "config" in construction_kwargs


def test_position_reader_uses_shared_redis_database_config() -> None:
    """Codex batch 8 P1 regression: PositionReader's cold-path
    DatabaseConfig must use the shared
    ``build_redis_database_config()`` helper so it inherits
    auth/TLS from ``settings.redis_url``. A separate
    DatabaseConfig built here would silently drop
    username/password/ssl on auth-protected Redis."""
    from unittest.mock import patch as _patch

    captured: dict[str, Any] = {}

    def fake_builder() -> Any:
        # Return a sentinel object so we can verify it's the
        # one PositionReader stored.
        sentinel = type("FakeDB", (), {})()
        captured["db"] = sentinel
        return sentinel

    with _patch(
        "msai.services.nautilus.projection.position_reader.build_redis_database_config",
        side_effect=fake_builder,
    ):
        reader = PositionReader(projection_state=ProjectionState())

    # The CacheConfig PositionReader builds must wrap the
    # exact DatabaseConfig the shared helper returned.
    assert reader._cache_config.database is captured["db"]  # noqa: SLF001


def test_build_adapter_serializer_uses_msgspec_module_not_string() -> None:
    """Regression test for Codex v5 P1: the ``encoding`` kwarg
    on ``MsgSpecSerializer`` must be the msgspec MODULE
    (``msgspec.msgpack``), not the string ``"msgpack"``. A
    string would fail at first encode.

    Approach: pass through to the real ``MsgSpecSerializer``
    constructor (which is a real Python wrapper around the
    Cython class) — if our code passes a string, the call
    raises before we get a chance to inspect it.
    """
    import msgspec

    reader, _ = _build_reader()
    captured: dict[str, Any] = {}

    class StandIn:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    with patch(
        "msai.services.nautilus.projection.position_reader.CacheDatabaseAdapter",
        StandIn,
    ):
        reader._build_adapter("MSAI-test")  # noqa: SLF001

    serializer = captured.get("serializer")
    assert serializer is not None
    # The real MsgSpecSerializer was constructed without raising,
    # which proves the encoding kwarg was the msgspec MODULE.
    # If it had been the string "msgpack", construction would
    # have failed (msgspec.msgpack is the module the serializer
    # uses for encode/decode internally per
    # nautilus_trader/serialization/serializer.pyx:58-59).
    assert serializer.__class__.__name__ == "MsgSpecSerializer"
    # Belt-and-suspenders: we don't accidentally pass the
    # module itself as the serializer.
    assert serializer is not msgspec.msgpack
