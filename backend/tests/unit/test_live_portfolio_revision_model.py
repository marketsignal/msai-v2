"""Unit tests for the LivePortfolioRevision model."""

from __future__ import annotations

from uuid import uuid4


def test_live_portfolio_revision_imports() -> None:
    from msai.models import LivePortfolioRevision

    rev = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=uuid4(),
        revision_number=1,
        composition_hash="a" * 64,
        is_frozen=False,
    )
    assert rev.revision_number == 1
    assert rev.is_frozen is False


def test_revision_required_columns_and_immutable_timestamp_shape() -> None:
    from msai.models import LivePortfolioRevision

    cols = {c.name: c for c in LivePortfolioRevision.__table__.columns}
    for name in ("portfolio_id", "revision_number", "composition_hash", "is_frozen"):
        assert cols[name].nullable is False, f"{name} must be NOT NULL"
    # Immutable on create — only created_at, no updated_at.
    assert "created_at" in cols
    assert "updated_at" not in cols
