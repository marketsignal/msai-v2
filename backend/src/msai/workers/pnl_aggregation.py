"""Daily PnL aggregation — arq cron job.

Runs at market close (20:30 UTC ≈ 4:30 PM ET during EDT).
Queries ``order_attempt_audits`` for the day's filled live orders
per deployment, aggregates trade count, and writes to
``strategy_daily_pnl``.

NOTE: Realized PnL computation requires entry/exit price matching
which is not yet implemented. This version writes ``pnl=0`` as a
placeholder and populates trade counts only. Full PnL accounting
will be added when fill-price persistence lands on the audit model.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.models.order_attempt_audit import OrderAttemptAudit
from msai.models.strategy_daily_pnl import StrategyDailyPnl

log = get_logger(__name__)


async def aggregate_daily_pnl(ctx: dict[str, Any], *, target_date: date | None = None) -> int:
    """Aggregate today's live fills into strategy_daily_pnl rows.

    Returns the number of rows written.
    """
    if target_date is None:
        target_date = datetime.now(UTC).date()

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    rows_written = 0
    try:
        async with factory() as session, session.begin():
            # Group fills by (deployment_id, strategy_id) for today
            groups = (
                await session.execute(
                    select(
                        OrderAttemptAudit.deployment_id,
                        OrderAttemptAudit.strategy_id,
                        func.count().label("num_trades"),
                    )
                    .where(
                        OrderAttemptAudit.is_live.is_(True),
                        OrderAttemptAudit.status.in_(("filled", "partially_filled")),
                        func.date(OrderAttemptAudit.ts_attempted) == target_date,
                    )
                    .group_by(
                        OrderAttemptAudit.deployment_id,
                        OrderAttemptAudit.strategy_id,
                    )
                )
            ).all()

            for dep_id, strat_id, num_trades in groups:
                if dep_id is None or strat_id is None:
                    continue

                # Check if row already exists (idempotent re-run)
                existing = (
                    await session.execute(
                        select(StrategyDailyPnl).where(
                            StrategyDailyPnl.deployment_id == dep_id,
                            StrategyDailyPnl.strategy_id == strat_id,
                            StrategyDailyPnl.date == target_date,
                        )
                    )
                ).scalar_one_or_none()

                if existing is not None:
                    existing.num_trades = num_trades
                else:
                    session.add(
                        StrategyDailyPnl(
                            strategy_id=strat_id,
                            deployment_id=dep_id,
                            date=target_date,
                            pnl=Decimal("0"),
                            cumulative_pnl=Decimal("0"),
                            num_trades=num_trades,
                            win_count=0,
                            loss_count=0,
                        )
                    )
                    rows_written += 1

        log.info("pnl_aggregation_complete", date=str(target_date), rows=rows_written)
    finally:
        await engine.dispose()

    return rows_written
