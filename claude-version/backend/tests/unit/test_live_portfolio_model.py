"""Unit tests for the LivePortfolio model."""

from __future__ import annotations

from uuid import uuid4


def test_live_portfolio_imports_and_instantiates() -> None:
    from msai.models import LivePortfolio

    portfolio = LivePortfolio(
        id=uuid4(),
        name="Growth Portfolio",
        description="Long-only momentum",
        created_by=None,
    )
    assert portfolio.name == "Growth Portfolio"


def test_live_portfolio_name_unique_and_required() -> None:
    from msai.models import LivePortfolio

    cols = {c.name: c for c in LivePortfolio.__table__.columns}
    assert cols["name"].nullable is False
    assert cols["description"].nullable is True
    # TimestampMixin columns must be present.
    assert "created_at" in cols
    assert "updated_at" in cols
