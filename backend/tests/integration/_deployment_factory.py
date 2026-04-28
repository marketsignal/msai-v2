"""Shared factory for constructing a valid ``LiveDeployment`` row in tests.

Post PR #29 (portfolio-per-account-live PR #2), ``LiveDeployment`` dropped
five legacy per-strategy columns (``strategy_code_hash``, ``config``,
``instruments``, ``config_hash``, ``instruments_signature``) and added
portfolio-scoped columns that are all ``NOT NULL``:

- ``deployment_slug`` (unique)
- ``identity_signature`` (unique, 64 chars)
- ``trader_id``
- ``strategy_id_full``
- ``account_id``
- ``ib_login_key`` (PR #30 added, PR #3 enforced)
- ``portfolio_revision_id`` (PR #31 enforced NOT NULL)
- ``message_bus_stream``

Every test that inserts a ``LiveDeployment`` row now needs to stage a
``LivePortfolio`` + ``LivePortfolioRevision`` first, then populate the
eight identity columns above. This factory encapsulates the full chain
so individual test files don't drift.

The factory also creates a ``LivePortfolioRevisionStrategy`` member row
on the revision, since the live supervisor (and the preflight gate)
treat zero-member revisions as fatal at spawn time. ``member_instruments``
defaults to ``["AAPL"]`` and can be overridden when a test needs the
member's ``instruments`` list to point at a specific symbol.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio import LivePortfolio
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy
from msai.models.strategy import Strategy
from msai.models.user import User
from msai.services.live.deployment_identity import (
    derive_message_bus_stream,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def make_live_deployment(
    session: AsyncSession,
    *,
    user: User | None = None,
    strategy: Strategy | None = None,
    user_id: UUID | None = None,
    strategy_id: UUID | None = None,
    strategy_class: str = "EMACrossStrategy",
    slug: str | None = None,
    account_id: str = "DU1234567",
    ib_login_key: str = "msai-paper-primary",
    paper_trading: bool = True,
    status: str = "running",
    member_instruments: list[str] | None = None,
) -> LiveDeployment:
    """Seed a ``LivePortfolio`` → ``LivePortfolioRevision`` →
    ``LivePortfolioRevisionStrategy`` → ``LiveDeployment`` chain that
    satisfies every ``NOT NULL`` constraint on the current schema.

    Pass EITHER ``user=`` and ``strategy=`` (full ORM instances) OR the
    lower-level ``user_id=`` and ``strategy_id=`` (UUIDs) — the latter is
    useful when the caller only has IDs (e.g. creating a SECOND deployment
    under an existing user). When neither tuple is provided, a fresh
    ``User`` + ``Strategy`` are auto-created in the same transaction —
    handy for preflight / orchestration tests that don't care which user
    or strategy backs the deployment.

    Flushes after each insert so the FKs resolve; does NOT commit — the
    caller owns the transaction boundary.

    ``member_instruments`` (default ``["AAPL"]``) populates the
    ``LivePortfolioRevisionStrategy.instruments`` array. The supervisor
    treats an empty member list (or a missing member row entirely) as a
    fatal portfolio-revision freeze bug, so the factory always creates
    one non-empty member row.

    The ``slug`` defaults to a fresh 16-char uuid suffix, guaranteeing a
    unique ``deployment_slug`` per call. ``identity_signature`` is also
    derived from fresh uuids so the unique index never collides across
    concurrent fixture calls in the same test module.
    """
    if user is not None:
        user_id = user.id
    if strategy is not None:
        strategy_id = strategy.id
        strategy_class = strategy.strategy_class

    if user_id is None and strategy_id is None:
        # Auto-create defaults in the caller's transaction (no commit —
        # see helper contract above). Fresh entra_id / email per call so
        # the unique indexes never collide across fixture invocations.
        auto_user = User(
            id=uuid4(),
            entra_id=f"factory-{uuid4().hex}",
            email=f"factory-{uuid4().hex}@test.com",
            role="trader",
        )
        session.add(auto_user)
        await session.flush()
        user_id = auto_user.id

        auto_strategy = Strategy(
            id=uuid4(),
            name=f"factory-strategy-{uuid4().hex[:8]}",
            file_path="strategies/example/ema_cross.py",
            strategy_class=strategy_class,
            created_by=auto_user.id,
        )
        session.add(auto_strategy)
        await session.flush()
        strategy_id = auto_strategy.id

    if user_id is None or strategy_id is None:
        raise ValueError(
            "make_live_deployment requires either (user, strategy), "
            "(user_id, strategy_id), or no user/strategy at all (auto-default)"
        )

    slug = slug or generate_deployment_slug()

    portfolio = LivePortfolio(
        id=uuid4(),
        name=f"test-portfolio-{slug}",
        description="test fixture",
        created_by=user_id,
    )
    session.add(portfolio)
    await session.flush()

    revision = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=portfolio.id,
        revision_number=1,
        composition_hash=uuid4().hex + uuid4().hex,  # 64 hex chars
        is_frozen=True,
    )
    session.add(revision)
    await session.flush()

    instruments = list(member_instruments) if member_instruments is not None else ["AAPL"]
    member = LivePortfolioRevisionStrategy(
        id=uuid4(),
        revision_id=revision.id,
        strategy_id=strategy_id,
        config={},
        instruments=instruments,
        weight=Decimal("1.0"),
        order_index=0,
    )
    session.add(member)
    await session.flush()

    deployment = LiveDeployment(
        id=uuid4(),
        strategy_id=strategy_id,
        status=status,
        paper_trading=paper_trading,
        started_by=user_id,
        deployment_slug=slug,
        identity_signature=uuid4().hex + uuid4().hex,
        trader_id=derive_trader_id(slug),
        strategy_id_full=derive_strategy_id_full(strategy_class, slug),
        account_id=account_id,
        ib_login_key=ib_login_key,
        portfolio_revision_id=revision.id,
        message_bus_stream=derive_message_bus_stream(slug),
    )
    session.add(deployment)
    await session.flush()
    return deployment
