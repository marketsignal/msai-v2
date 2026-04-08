"""Integration tests for the ``OrderAttemptAudit`` model (Phase 1 task 1.2).

Verifies the schema, constraints, and update-by-``client_order_id`` state
machine that the live audit hook (Task 1.11) and the backtest runner
(Task 4.4) write to.

SAFETY: provisions its own dedicated ``PostgresContainer`` (same pattern
as the other Phase 1 integration tests) so the destructive
``drop_all/create_all`` fixture can never touch a configured DATABASE_URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import (
    Backtest,
    Base,
    LiveDeployment,
    OrderAttemptAudit,
    Strategy,
    User,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Dedicated Postgres testcontainer for this module only."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def user_strategy(session: AsyncSession) -> AsyncIterator[tuple[User, Strategy]]:
    user = User(
        id=uuid4(),
        entra_id=f"oa-{uuid4().hex}",
        email=f"oa-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategy = Strategy(
        id=uuid4(),
        name="ema-cross",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        created_by=user.id,
    )
    session.add(strategy)
    await session.commit()
    yield user, strategy


@pytest_asyncio.fixture
async def deployment(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
) -> LiveDeployment:
    user, strategy = user_strategy
    slug = "abcd1234abcd1234"
    dep = LiveDeployment(
        id=uuid4(),
        strategy_id=strategy.id,
        strategy_code_hash="deadbeef" * 8,
        config={"fast": 10, "slow": 20},
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
    session.add(dep)
    await session.commit()
    return dep


@pytest_asyncio.fixture
async def backtest(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
) -> Backtest:
    user, strategy = user_strategy
    bt = Backtest(
        id=uuid4(),
        strategy_id=strategy.id,
        strategy_code_hash="deadbeef" * 8,
        config={"fast": 10, "slow": 20},
        instruments=["AAPL.NASDAQ"],
        start_date=_utcnow().date(),
        end_date=_utcnow().date(),
        status="completed",
        progress=100,
        created_by=user.id,
    )
    session.add(bt)
    await session.commit()
    return bt


def _make_audit(
    *,
    deployment_id=None,
    backtest_id=None,
    strategy_id,
    client_order_id: str = "msai-001",
    side: str = "BUY",
    quantity: Decimal = Decimal("10"),
    price: Decimal | None = Decimal("100.00"),
    order_type: str = "LIMIT",
    status: str = "submitted",
    is_live: bool = True,
) -> OrderAttemptAudit:
    return OrderAttemptAudit(
        id=uuid4(),
        client_order_id=client_order_id,
        deployment_id=deployment_id,
        backtest_id=backtest_id,
        strategy_id=strategy_id,
        strategy_code_hash="deadbeef" * 8,
        instrument_id="AAPL.NASDAQ",
        side=side,
        quantity=quantity,
        price=price,
        order_type=order_type,
        ts_attempted=_utcnow(),
        status=status,
        is_live=is_live,
    )


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_live_audit_round_trips(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """A live deployment audit row inserts and round-trips every column."""
    _, strategy = user_strategy
    audit = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id="msai-live-001",
    )
    session.add(audit)
    await session.commit()

    fetched = await session.get(OrderAttemptAudit, audit.id)
    assert fetched is not None
    assert fetched.client_order_id == "msai-live-001"
    assert fetched.deployment_id == deployment.id
    assert fetched.backtest_id is None
    assert fetched.strategy_id == strategy.id
    assert fetched.instrument_id == "AAPL.NASDAQ"
    assert fetched.side == "BUY"
    assert fetched.quantity == Decimal("10")
    assert fetched.price == Decimal("100.00")
    assert fetched.order_type == "LIMIT"
    assert fetched.status == "submitted"
    assert fetched.is_live is True
    assert fetched.broker_order_id is None
    assert fetched.reason is None


@pytest.mark.asyncio
async def test_insert_backtest_audit_round_trips(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    backtest: Backtest,
) -> None:
    """A backtest audit row populates ``backtest_id`` instead of ``deployment_id``."""
    _, strategy = user_strategy
    audit = _make_audit(
        backtest_id=backtest.id,
        strategy_id=strategy.id,
        client_order_id="msai-bt-001",
        is_live=False,
    )
    session.add(audit)
    await session.commit()

    fetched = await session.get(OrderAttemptAudit, audit.id)
    assert fetched is not None
    assert fetched.deployment_id is None
    assert fetched.backtest_id == backtest.id
    assert fetched.is_live is False


# ---------------------------------------------------------------------------
# CHECK constraint: at least one of deployment_id / backtest_id must be set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_constraint_rejects_both_null(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
) -> None:
    """Decision: every audit row MUST belong to either a live deployment
    or a backtest. Both NULL is meaningless and must be rejected at the
    DB layer (not just the application layer)."""
    _, strategy = user_strategy
    orphan = _make_audit(
        deployment_id=None,
        backtest_id=None,
        strategy_id=strategy.id,
        client_order_id="orphan-001",
    )
    session.add(orphan)
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_check_constraint_rejects_both_populated(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
    backtest: Backtest,
) -> None:
    """Codex Task 1.2 iter2 P2 fix: exactly ONE of
    ``deployment_id`` / ``backtest_id`` must be non-NULL. Populating
    both creates an ambiguous audit row downstream reconciliation and
    analytics cannot classify, so the DB layer must reject it via the
    XOR CHECK constraint (``(dep IS NOT NULL) != (bt IS NOT NULL)``).

    The earlier constraint was ``(dep IS NOT NULL) OR (bt IS NOT NULL)``
    which incorrectly accepted rows with both populated.
    """
    _, strategy = user_strategy
    ambiguous = _make_audit(
        deployment_id=deployment.id,
        backtest_id=backtest.id,
        strategy_id=strategy.id,
        client_order_id="ambiguous-001",
    )
    session.add(ambiguous)
    with pytest.raises(IntegrityError):
        await session.commit()


# ---------------------------------------------------------------------------
# UNIQUE(client_order_id) — the correlation key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_order_id_is_unique(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """``client_order_id`` is the correlation key the audit hook uses to
    update a row through its state machine. Two rows with the same id
    would break that lookup, so the constraint must be enforced at the
    DB layer."""
    _, strategy = user_strategy
    a = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id="dup-001",
    )
    session.add(a)
    await session.commit()

    b = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id="dup-001",
    )
    session.add(b)
    with pytest.raises(IntegrityError):
        await session.commit()


# ---------------------------------------------------------------------------
# State machine: lookup by client_order_id, update through statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_machine_submitted_to_filled(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """The audit hook writes ``submitted`` first, then looks the row up
    by ``client_order_id`` and updates through accepted → filled. The
    model must support this update-by-correlation-key flow."""
    _, strategy = user_strategy
    coid = "msai-state-001"

    initial = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id=coid,
        status="submitted",
    )
    session.add(initial)
    await session.commit()

    # Look up by client_order_id, transition to accepted
    result = await session.execute(
        select(OrderAttemptAudit).where(OrderAttemptAudit.client_order_id == coid)
    )
    row = result.scalar_one()
    row.status = "accepted"
    row.broker_order_id = "ib-99999"
    await session.commit()

    # Re-lookup, transition to filled
    result = await session.execute(
        select(OrderAttemptAudit).where(OrderAttemptAudit.client_order_id == coid)
    )
    row = result.scalar_one()
    assert row.status == "accepted"
    assert row.broker_order_id == "ib-99999"
    row.status = "filled"
    await session.commit()

    # Final state
    result = await session.execute(
        select(OrderAttemptAudit).where(OrderAttemptAudit.client_order_id == coid)
    )
    final = result.scalar_one()
    assert final.status == "filled"
    assert final.broker_order_id == "ib-99999"


@pytest.mark.asyncio
async def test_state_machine_submitted_to_rejected(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """A rejected order keeps its ``client_order_id``, gets ``status='rejected'``,
    and stores the reason text from the broker."""
    _, strategy = user_strategy
    coid = "msai-rejected-001"

    initial = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id=coid,
    )
    session.add(initial)
    await session.commit()

    result = await session.execute(
        select(OrderAttemptAudit).where(OrderAttemptAudit.client_order_id == coid)
    )
    row = result.scalar_one()
    row.status = "rejected"
    row.reason = "Insufficient buying power"
    await session.commit()

    result = await session.execute(
        select(OrderAttemptAudit).where(OrderAttemptAudit.client_order_id == coid)
    )
    final = result.scalar_one()
    assert final.status == "rejected"
    assert final.reason == "Insufficient buying power"


@pytest.mark.asyncio
async def test_denied_by_risk_engine(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """The risk engine pre-trade check writes a ``denied`` row when an
    order is blocked before submission. The audit hook never sees it
    on the wire — the audit is the only record."""
    _, strategy = user_strategy
    audit = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id="msai-denied-001",
        status="denied",
    )
    audit.reason = "Daily loss limit reached"
    session.add(audit)
    await session.commit()

    fetched = await session.get(OrderAttemptAudit, audit.id)
    assert fetched is not None
    assert fetched.status == "denied"
    assert fetched.reason == "Daily loss limit reached"
    # Denied orders are NOT submitted to the broker, so broker_order_id
    # stays NULL even though the row carries deployment_id.
    assert fetched.broker_order_id is None


# ---------------------------------------------------------------------------
# Decimal precision (no float drift in price/quantity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decimal_precision_preserved(
    session: AsyncSession,
    user_strategy: tuple[User, Strategy],
    deployment: LiveDeployment,
) -> None:
    """Quantity and price round-trip as ``Decimal`` with full precision —
    we never want float drift on a pricing/quantity field."""
    _, strategy = user_strategy
    audit = _make_audit(
        deployment_id=deployment.id,
        strategy_id=strategy.id,
        client_order_id="msai-decimal-001",
        quantity=Decimal("0.12345678"),
        price=Decimal("12345.67891234"),
    )
    session.add(audit)
    await session.commit()

    fetched = await session.get(OrderAttemptAudit, audit.id)
    assert fetched is not None
    assert fetched.quantity == Decimal("0.12345678")
    assert fetched.price == Decimal("12345.67891234")


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indexes_exist(session: AsyncSession) -> None:
    """The columns the audit hook queries on must be indexed:
    ``client_order_id`` (correlation key), ``deployment_id`` (FK lookup),
    ``backtest_id`` (FK lookup), ``strategy_id`` (FK lookup),
    ``instrument_id`` (group-by for analytics), ``broker_order_id``
    (broker-side reconciliation)."""
    from sqlalchemy import inspect

    def _inspect(sync_conn) -> set[str]:
        insp = inspect(sync_conn)
        return {idx["name"] for idx in insp.get_indexes("order_attempt_audits")}

    bind = session.bind
    assert bind is not None
    async with bind.connect() as conn:
        index_names = await conn.run_sync(_inspect)

    expected = {
        "ix_order_attempt_audits_client_order_id",
        "ix_order_attempt_audits_deployment_id",
        "ix_order_attempt_audits_backtest_id",
        "ix_order_attempt_audits_strategy_id",
        "ix_order_attempt_audits_instrument_id",
        "ix_order_attempt_audits_broker_order_id",
    }
    missing = expected - index_names
    assert not missing, f"missing indexes on order_attempt_audits: {missing}"
