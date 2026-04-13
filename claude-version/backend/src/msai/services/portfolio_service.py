"""Portfolio management service -- CRUD for portfolios, allocations, and runs.

Provides methods to create portfolios with weighted strategy allocations,
list/get portfolios, create portfolio-level backtest runs, and query run history.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.logging import get_logger
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_run import PortfolioRun
from msai.schemas.portfolio import PortfolioCreate, PortfolioRunCreate

log = get_logger(__name__)


class PortfolioService:
    """Manages portfolio lifecycle: creation, allocation, and combined backtest runs."""

    async def create(
        self,
        session: AsyncSession,
        data: PortfolioCreate,
        user_id: UUID | None = None,
    ) -> Portfolio:
        """Create a portfolio with its allocation rows.

        Args:
            session: Active async database session.
            data: Validated portfolio creation payload including allocations.
            user_id: Optional user UUID for the ``created_by`` field.

        Returns:
            The newly created :class:`Portfolio` row (flushed, not committed).
        """
        portfolio = Portfolio(
            name=data.name,
            description=data.description,
            objective=data.objective,
            base_capital=data.base_capital,
            requested_leverage=data.requested_leverage,
            benchmark_symbol=data.benchmark_symbol,
            created_by=user_id,
        )
        session.add(portfolio)
        await session.flush()

        # Validate: no duplicate candidate IDs
        seen_ids = set()
        for alloc in data.allocations:
            if alloc.candidate_id in seen_ids:
                raise ValueError(
                    f"Duplicate candidate {alloc.candidate_id} in allocations"
                )
            seen_ids.add(alloc.candidate_id)

        # Validate all candidate IDs exist before inserting allocations
        for alloc in data.allocations:
            candidate = await session.get(GraduationCandidate, alloc.candidate_id)
            if candidate is None:
                raise ValueError(
                    f"Graduation candidate {alloc.candidate_id} not found"
                )

        for alloc in data.allocations:
            allocation = PortfolioAllocation(
                portfolio_id=portfolio.id,
                candidate_id=alloc.candidate_id,
                weight=alloc.weight,
            )
            session.add(allocation)

        await session.flush()

        log.info(
            "portfolio_created",
            portfolio_id=str(portfolio.id),
            name=data.name,
            num_allocations=len(data.allocations),
        )
        return portfolio

    async def list(
        self,
        session: AsyncSession,
        limit: int = 100,
    ) -> list[Portfolio]:
        """List portfolios ordered by creation time (newest first).

        Args:
            session: Active async database session.
            limit: Maximum number of rows to return.

        Returns:
            A list of :class:`Portfolio` rows.
        """
        stmt = (
            select(Portfolio)
            .order_by(Portfolio.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> Portfolio:
        """Get a single portfolio by ID. Raises ValueError if not found.

        Args:
            session: Active async database session.
            portfolio_id: Primary key of the portfolio row.

        Returns:
            The :class:`Portfolio` row.

        Raises:
            ValueError: If the portfolio does not exist.
        """
        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")
        return portfolio

    async def get_allocations(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
    ) -> list[PortfolioAllocation]:
        """List allocations for a portfolio.

        Args:
            session: Active async database session.
            portfolio_id: FK to the owning portfolio.

        Returns:
            A list of :class:`PortfolioAllocation` rows for the given portfolio.
        """
        stmt = (
            select(PortfolioAllocation)
            .where(PortfolioAllocation.portfolio_id == portfolio_id)
            .order_by(PortfolioAllocation.created_at)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def create_run(
        self,
        session: AsyncSession,
        portfolio_id: UUID,
        data: PortfolioRunCreate,
        user_id: UUID | None = None,
    ) -> PortfolioRun:
        """Create a portfolio backtest run.

        Args:
            session: Active async database session.
            portfolio_id: FK to the portfolio being evaluated.
            data: Validated run creation payload (date range).
            user_id: Optional user UUID for the ``created_by`` field.

        Returns:
            The newly created :class:`PortfolioRun` row (flushed, not committed).

        Raises:
            ValueError: If the referenced portfolio does not exist.
        """
        # Verify portfolio exists
        portfolio = await session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")

        run = PortfolioRun(
            portfolio_id=portfolio_id,
            start_date=data.start_date,
            end_date=data.end_date,
            status="pending",
            created_by=user_id,
        )
        session.add(run)
        await session.flush()

        log.info(
            "portfolio_run_created",
            run_id=str(run.id),
            portfolio_id=str(portfolio_id),
            start_date=str(data.start_date),
            end_date=str(data.end_date),
        )
        return run

    async def list_runs(
        self,
        session: AsyncSession,
        portfolio_id: UUID | None = None,
        limit: int = 100,
    ) -> list[PortfolioRun]:
        """List portfolio runs, optionally filtered by portfolio.

        Args:
            session: Active async database session.
            portfolio_id: Optional FK filter. If provided, only runs for this
                portfolio are returned.
            limit: Maximum number of rows to return.

        Returns:
            A list of :class:`PortfolioRun` rows.
        """
        stmt = (
            select(PortfolioRun)
            .order_by(PortfolioRun.created_at.desc())
            .limit(limit)
        )
        if portfolio_id is not None:
            stmt = stmt.where(PortfolioRun.portfolio_id == portfolio_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_run(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> PortfolioRun:
        """Get a single portfolio run by ID. Raises ValueError if not found.

        Args:
            session: Active async database session.
            run_id: Primary key of the run row.

        Returns:
            The :class:`PortfolioRun` row.

        Raises:
            ValueError: If the run does not exist.
        """
        run = await session.get(PortfolioRun, run_id)
        if run is None:
            raise ValueError(f"Portfolio run {run_id} not found")
        return run

    async def count(self, session: AsyncSession) -> int:
        """Return the total number of portfolios."""
        result = await session.execute(select(func.count()).select_from(Portfolio))
        return result.scalar_one()

    async def count_runs(
        self,
        session: AsyncSession,
        portfolio_id: UUID | None = None,
    ) -> int:
        """Return the total number of portfolio runs, optionally filtered."""
        stmt = select(func.count()).select_from(PortfolioRun)
        if portfolio_id is not None:
            stmt = stmt.where(PortfolioRun.portfolio_id == portfolio_id)
        result = await session.execute(stmt)
        return result.scalar_one()
