"""Integration tests for ``OrderAuditWriter`` (Phase 1 task 1.11).

Exercises the full order lifecycle state machine against a real
Postgres container. The writer is pure async with no Nautilus
imports, so these tests don't need any IB Gateway mocking — they
simulate the lifecycle events by calling the writer's methods
directly in the order a Strategy mixin would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Backtest, Base, LiveDeployment, Strategy, User
from msai.services.nautilus.audit_hook import (
    OrderAuditWriter,
    OrderSubmittedFacts,
    lookup_by_client_order_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def fixtures(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    """Seed a User/Strategy/LiveDeployment/Backtest chain so every
    audit row has valid foreign keys."""
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"ah-{uuid4().hex}",
            email=f"ah-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name="audit-test",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        slug = uuid4().hex[:16]
        deployment = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            strategy_code_hash="deadbeef" * 8,
            config={},
            instruments=["AAPL.NASDAQ"],
            status="running",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature="f" * 64,
            trader_id=f"MSAI-{slug}",
            strategy_id_full=f"EMACrossStrategy-{slug}",
            account_id="DU1234567",
            message_bus_stream=f"trader-MSAI-{slug}-stream",
            config_hash="cafebabe" * 8,
            instruments_signature="AAPL.NASDAQ",
        )
        session.add(deployment)

        backtest = Backtest(
            id=uuid4(),
            strategy_id=strategy.id,
            strategy_code_hash="deadbeef" * 8,
            config={},
            instruments=["AAPL.NASDAQ"],
            start_date=datetime.now(UTC).date(),
            end_date=datetime.now(UTC).date(),
            status="completed",
            progress=100,
            created_by=user.id,
        )
        session.add(backtest)
        await session.commit()

        return {"strategy": strategy, "deployment": deployment, "backtest": backtest}


def _facts(
    *,
    client_order_id: str,
    strategy_id,
    deployment_id=None,
    backtest_id=None,
    is_live: bool = True,
    quantity: Decimal = Decimal("10"),
    price: Decimal | None = Decimal("100.00"),
) -> OrderSubmittedFacts:
    return OrderSubmittedFacts(
        client_order_id=client_order_id,
        deployment_id=deployment_id,
        backtest_id=backtest_id,
        strategy_id=strategy_id,
        strategy_code_hash="deadbeef" * 8,
        instrument_id="AAPL.NASDAQ",
        side="BUY",
        quantity=quantity,
        price=price,
        order_type="LIMIT",
        ts_attempted=datetime.now(UTC),
        is_live=is_live,
    )


# ---------------------------------------------------------------------------
# write_submitted — happy path + XOR guard
# ---------------------------------------------------------------------------


class TestWriteSubmitted:
    @pytest.mark.asyncio
    async def test_inserts_live_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        row_id = await writer.write_submitted(
            _facts(
                client_order_id="live-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )
        assert row_id is not None

        row = await lookup_by_client_order_id(session_factory, "live-001")
        assert row is not None
        assert row.status == "submitted"
        assert row.deployment_id == deployment.id  # type: ignore[union-attr]
        assert row.backtest_id is None
        assert row.is_live is True
        assert row.quantity == Decimal("10")
        assert row.price == Decimal("100.00")
        assert row.side == "BUY"

    @pytest.mark.asyncio
    async def test_inserts_backtest_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        backtest = fixtures["backtest"]

        await writer.write_submitted(
            _facts(
                client_order_id="bt-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                backtest_id=backtest.id,  # type: ignore[union-attr]
                is_live=False,
            )
        )

        row = await lookup_by_client_order_id(session_factory, "bt-001")
        assert row is not None
        assert row.deployment_id is None
        assert row.backtest_id == backtest.id  # type: ignore[union-attr]
        assert row.is_live is False

    @pytest.mark.asyncio
    async def test_xor_violation_both_null_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        """Application-level XOR check happens before the DB would
        also reject it — surfaces the client_order_id context in the
        exception message so the Strategy hook can log it properly."""
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]

        with pytest.raises(ValueError, match="exactly one"):
            await writer.write_submitted(
                _facts(
                    client_order_id="orphan",
                    strategy_id=strategy.id,  # type: ignore[union-attr]
                )
            )

    @pytest.mark.asyncio
    async def test_xor_violation_both_populated_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]
        backtest = fixtures["backtest"]

        with pytest.raises(ValueError, match="exactly one"):
            await writer.write_submitted(
                _facts(
                    client_order_id="ambiguous",
                    strategy_id=strategy.id,  # type: ignore[union-attr]
                    deployment_id=deployment.id,  # type: ignore[union-attr]
                    backtest_id=backtest.id,  # type: ignore[union-attr]
                )
            )


# ---------------------------------------------------------------------------
# State machine: submitted → accepted → filled
# ---------------------------------------------------------------------------


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_full_happy_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        await writer.write_submitted(
            _facts(
                client_order_id="happy-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )

        await writer.update_accepted("happy-001", broker_order_id="ib-12345")
        row = await lookup_by_client_order_id(session_factory, "happy-001")
        assert row is not None
        assert row.status == "accepted"
        assert row.broker_order_id == "ib-12345"

        await writer.update_filled("happy-001")
        row = await lookup_by_client_order_id(session_factory, "happy-001")
        assert row is not None
        assert row.status == "filled"
        # broker_order_id MUST be preserved from the accepted step —
        # update_filled() must not clobber it.
        assert row.broker_order_id == "ib-12345"

    @pytest.mark.asyncio
    async def test_rejected_with_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        await writer.write_submitted(
            _facts(
                client_order_id="rej-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )

        await writer.update_rejected("rej-001", reason="Insufficient buying power")

        row = await lookup_by_client_order_id(session_factory, "rej-001")
        assert row is not None
        assert row.status == "rejected"
        assert row.reason == "Insufficient buying power"
        # broker never acknowledged — broker_order_id is still NULL
        assert row.broker_order_id is None

    @pytest.mark.asyncio
    async def test_cancelled_transition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        await writer.write_submitted(
            _facts(
                client_order_id="cxl-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )
        await writer.update_accepted("cxl-001", broker_order_id="ib-99")
        await writer.update_cancelled("cxl-001", reason="user abort")

        row = await lookup_by_client_order_id(session_factory, "cxl-001")
        assert row is not None
        assert row.status == "cancelled"
        assert row.reason == "user abort"
        assert row.broker_order_id == "ib-99"  # preserved

    @pytest.mark.asyncio
    async def test_partially_filled_is_not_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        """A partial fill is a mid-life state — further fills can flip
        it to ``filled``. Make sure the writer preserves earlier
        fields like ``broker_order_id``."""
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        await writer.write_submitted(
            _facts(
                client_order_id="pf-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )
        await writer.update_accepted("pf-001", broker_order_id="ib-77")
        await writer.update_partially_filled("pf-001")

        row = await lookup_by_client_order_id(session_factory, "pf-001")
        assert row is not None
        assert row.status == "partially_filled"
        assert row.broker_order_id == "ib-77"

        # Second fill completes the order
        await writer.update_filled("pf-001")
        row = await lookup_by_client_order_id(session_factory, "pf-001")
        assert row is not None
        assert row.status == "filled"
        assert row.broker_order_id == "ib-77"

    @pytest.mark.asyncio
    async def test_denied_by_risk_engine(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fixtures: dict[str, object],
    ) -> None:
        """Risk engine denies an order before submission. The row is
        written with ``deployment_id`` but NEVER touches the broker,
        so ``broker_order_id`` stays NULL forever."""
        writer = OrderAuditWriter(db=session_factory)
        strategy = fixtures["strategy"]
        deployment = fixtures["deployment"]

        await writer.write_submitted(
            _facts(
                client_order_id="den-001",
                strategy_id=strategy.id,  # type: ignore[union-attr]
                deployment_id=deployment.id,  # type: ignore[union-attr]
            )
        )

        await writer.update_denied("den-001", reason="Daily loss limit reached")

        row = await lookup_by_client_order_id(session_factory, "den-001")
        assert row is not None
        assert row.status == "denied"
        assert row.reason == "Daily loss limit reached"
        assert row.broker_order_id is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_update_unknown_client_order_id_does_not_raise(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Update methods are best-effort — if no row matches, the
        writer logs and returns without raising. Strategy event
        handlers can't do anything meaningful with an exception here
        and audit is already a recording path."""
        writer = OrderAuditWriter(db=session_factory)
        await writer.update_accepted("never-existed", broker_order_id="ib-42")
        await writer.update_filled("never-existed")
        await writer.update_rejected("never-existed", reason="nope")

        assert await lookup_by_client_order_id(session_factory, "never-existed") is None

    @pytest.mark.asyncio
    async def test_lookup_returns_none_for_unknown_id(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await lookup_by_client_order_id(session_factory, "nope") is None
