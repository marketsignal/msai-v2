"""Unit tests for the M:N LivePortfolioRevisionStrategy bridge."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4


def test_revision_strategy_imports() -> None:
    from msai.models import LivePortfolioRevisionStrategy

    rs = LivePortfolioRevisionStrategy(
        id=uuid4(),
        revision_id=uuid4(),
        strategy_id=uuid4(),
        config={"fast": 10},
        instruments=["AAPL.NASDAQ"],
        weight=Decimal("0.25"),
        order_index=0,
    )
    assert rs.config == {"fast": 10}


def test_revision_strategy_required_columns() -> None:
    from msai.models import LivePortfolioRevisionStrategy

    cols = {c.name: c for c in LivePortfolioRevisionStrategy.__table__.columns}
    for name in ("revision_id", "strategy_id", "config", "instruments", "weight", "order_index"):
        assert cols[name].nullable is False
    # Immutable on create — created_at only, no updated_at.
    assert "updated_at" not in cols
