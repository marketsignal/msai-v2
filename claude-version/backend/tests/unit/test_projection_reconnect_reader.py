"""Unit tests for the reconnect snapshot readers (Phase 2 #4).

These tests verify the FILTER CONTRACT using mocked AsyncSessions
— the SQL expression is asserted via ``session.execute`` call
inspection. Full SQL-dialect correctness is covered by the
integration test against a real Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from msai.services.nautilus.projection.reconnect_reader import (
    OPEN_ORDER_STATUSES,
    load_open_orders_for_deployment,
    load_recent_trades_for_deployment,
)


def _make_session_returning(rows: list[object]) -> MagicMock:
    """Build an AsyncSession that returns ``rows`` from the next
    ``execute(...).scalars().all()`` chain."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _make_fake_audit(**overrides: object) -> MagicMock:
    """Build a MagicMock that quacks like an OrderAttemptAudit row."""
    row = MagicMock()
    row.id = overrides.get("id", uuid4())
    row.client_order_id = overrides.get("client_order_id", "O-1")
    row.instrument_id = overrides.get("instrument_id", "AAPL.NASDAQ")
    row.side = overrides.get("side", "BUY")
    row.quantity = overrides.get("quantity", "10")
    row.price = overrides.get("price", "150.00")
    row.order_type = overrides.get("order_type", "MARKET")
    row.status = overrides.get("status", "submitted")
    row.reason = overrides.get("reason")
    row.broker_order_id = overrides.get("broker_order_id")
    ts = MagicMock()
    ts.isoformat = MagicMock(return_value="2026-04-16T10:00:00+00:00")
    row.ts_attempted = overrides.get("ts_attempted", ts)
    return row


def _make_fake_trade(**overrides: object) -> MagicMock:
    """Build a MagicMock that quacks like a Trade row."""
    row = MagicMock()
    row.id = overrides.get("id", uuid4())
    row.deployment_id = overrides.get("deployment_id", uuid4())
    row.instrument = overrides.get("instrument", "AAPL.NASDAQ")
    row.side = overrides.get("side", "BUY")
    row.quantity = overrides.get("quantity", "10")
    row.price = overrides.get("price", "150.00")
    row.commission = overrides.get("commission", "0.05")
    row.broker_trade_id = overrides.get("broker_trade_id", "T-1")
    row.client_order_id = overrides.get("client_order_id", "O-1")
    row.pnl = overrides.get("pnl")
    row.is_live = overrides.get("is_live", True)
    ts = MagicMock()
    ts.isoformat = MagicMock(return_value="2026-04-16T10:00:00+00:00")
    row.executed_at = overrides.get("executed_at", ts)
    return row


class TestLoadOpenOrdersForDeployment:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self) -> None:
        session = _make_session_returning([])
        result = await load_open_orders_for_deployment(session, uuid4())
        assert result == []
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_filters_by_deployment_id_and_open_statuses(self) -> None:
        """The WHERE clause MUST restrict to (deployment_id == arg) AND
        (status IN open statuses). A regression here would leak terminal
        orders into the UI reconnect ribbon."""
        session = _make_session_returning([])
        deployment_id = uuid4()

        await load_open_orders_for_deployment(session, deployment_id)

        # Extract the statement passed to execute()
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "deployment_id" in compiled
        # All open statuses must appear in the IN clause.
        for status in OPEN_ORDER_STATUSES:
            assert status in compiled
        # Terminal statuses MUST NOT appear as literal filter values
        # (they live in the column; we just mustn't filter on them).
        assert "'filled'" not in compiled
        assert "'cancelled'" not in compiled
        assert "'rejected'" not in compiled
        assert "'denied'" not in compiled
        # Ordered newest-first.
        assert "ORDER BY" in compiled.upper()
        assert "DESC" in compiled.upper()

    @pytest.mark.asyncio
    async def test_serializes_row_shape(self) -> None:
        """Each returned dict must have all the fields the UI needs,
        with numerics as strings and timestamps as ISO-8601."""
        row = _make_fake_audit()
        session = _make_session_returning([row])

        result = await load_open_orders_for_deployment(session, uuid4())
        assert len(result) == 1
        entry = result[0]
        # Contract: UI reads these keys directly.
        expected_keys = {
            "id",
            "client_order_id",
            "instrument_id",
            "side",
            "quantity",
            "price",
            "order_type",
            "status",
            "reason",
            "broker_order_id",
            "ts_attempted",
        }
        assert set(entry.keys()) == expected_keys
        # ID and ts are stringified.
        assert isinstance(entry["id"], str)
        assert entry["ts_attempted"] == "2026-04-16T10:00:00+00:00"

    @pytest.mark.asyncio
    async def test_open_order_statuses_constant_matches_audit_state_machine(
        self,
    ) -> None:
        """The OPEN_ORDER_STATUSES tuple pins the subset of the audit
        state machine that's 'still in flight'. Changing the audit
        state machine without updating this list would drop orders
        from the reconnect ribbon silently."""
        assert OPEN_ORDER_STATUSES == ("submitted", "accepted", "partially_filled")


class TestLoadRecentTradesForDeployment:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self) -> None:
        session = _make_session_returning([])
        result = await load_recent_trades_for_deployment(session, uuid4())
        assert result == []

    @pytest.mark.asyncio
    async def test_query_filters_by_deployment_id_and_orders_desc(self) -> None:
        session = _make_session_returning([])
        deployment_id = uuid4()

        await load_recent_trades_for_deployment(session, deployment_id, limit=25)

        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "deployment_id" in compiled
        assert "ORDER BY" in compiled.upper()
        assert "DESC" in compiled.upper()
        assert "LIMIT" in compiled.upper()

    @pytest.mark.asyncio
    async def test_zero_limit_short_circuits_without_db(self) -> None:
        """``limit=0`` / negative MUST avoid a DB round-trip so callers
        can disable the trade tail cheaply (e.g., UI preference)."""
        session = _make_session_returning([])

        assert await load_recent_trades_for_deployment(session, uuid4(), limit=0) == []
        assert await load_recent_trades_for_deployment(session, uuid4(), limit=-1) == []
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_serializes_row_shape(self) -> None:
        dep = uuid4()
        row = _make_fake_trade(deployment_id=dep)
        session = _make_session_returning([row])

        result = await load_recent_trades_for_deployment(session, uuid4())
        assert len(result) == 1
        entry = result[0]
        expected_keys = {
            "id",
            "deployment_id",
            "instrument",
            "side",
            "quantity",
            "price",
            "commission",
            "broker_trade_id",
            "client_order_id",
            "pnl",
            "is_live",
            "executed_at",
        }
        assert set(entry.keys()) == expected_keys
        assert entry["deployment_id"] == str(dep)
        assert entry["is_live"] is True
        assert entry["pnl"] is None  # preserved as None, not "None"
        assert entry["executed_at"] == "2026-04-16T10:00:00+00:00"

    @pytest.mark.asyncio
    async def test_null_deployment_id_serializes_as_none(self) -> None:
        """Edge case: ``Trade.deployment_id`` is nullable (backtest
        rows). Our reader filter excludes those, but defense-in-depth:
        if one slips through, serialize to ``None`` not ``'None'``."""
        row = _make_fake_trade(deployment_id=None)
        session = _make_session_returning([row])

        result = await load_recent_trades_for_deployment(session, uuid4())
        assert result[0]["deployment_id"] is None


class TestReaderDocstringInvariants:
    """Meta-tests to pin council-mandated contract markers in
    docstrings so future edits don't drop them silently."""

    def test_reader_module_documents_reconnect_purpose(self) -> None:
        from msai.services.nautilus.projection import reconnect_reader

        doc = reconnect_reader.__doc__ or ""
        assert "reconnect" in doc.lower()
        assert "OrderAttemptAudit" in doc
        assert "Trade" in doc

    def test_load_open_orders_documents_terminal_exclusion(self) -> None:
        doc = load_open_orders_for_deployment.__doc__ or ""
        assert "open" in doc.lower() or "in-flight" in doc.lower() or "still-open" in doc.lower()
