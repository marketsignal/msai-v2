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
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio import LivePortfolio
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.services.live.deployment_identity import (
    derive_message_bus_stream,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.strategy import Strategy
    from msai.models.user import User


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
) -> LiveDeployment:
    """Seed a ``LivePortfolio`` → ``LivePortfolioRevision`` → ``LiveDeployment``
    chain that satisfies every ``NOT NULL`` constraint on the current schema.

    Pass EITHER ``user=`` and ``strategy=`` (full ORM instances) OR the
    lower-level ``user_id=`` and ``strategy_id=`` (UUIDs) — the latter is
    useful when the caller only has IDs (e.g. creating a SECOND deployment
    under an existing user).

    Flushes after each insert so the FKs resolve; does NOT commit — the
    caller owns the transaction boundary.

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
    if user_id is None or strategy_id is None:
        raise ValueError(
            "make_live_deployment requires either (user, strategy) or (user_id, strategy_id)"
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
