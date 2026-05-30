"""Validation tests for ``PortfolioStartRequest``.

Codex iter 20 P2 reverted the iter-15 ``ib_login_key='default'``
rejection — it broke warm-restart of legacy rows backfilled by
migration ``t8o9p0q1r2s3`` (sending a real key creates a different
``identity_signature`` and hits the
``(portfolio_revision_id, account_id)`` conflict path). The
supervisor's ``is_routed`` predicate plus startup warnings handle
the operator-typo concern at the right layer.

These tests now lock in:
- ``ib_login_key='default'`` is ACCEPTED (legacy warm-restart path).
- ``account_id`` whitespace is stripped / rejected (iter 16 P2).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.live import PortfolioStartRequest


def test_portfolio_start_request_accepts_default_login_key() -> None:
    """Codex iter 20 P2: ``'default'`` is the migration sentinel used
    by warm-restart of legacy rows. The schema MUST accept it; the
    supervisor's ``is_routed`` predicate routes ``default`` to
    ``settings.ib_host/port`` (legacy single-gateway fallback)."""
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DUP733214",
        paper_trading=True,
        ib_login_key="default",
    )
    assert req.ib_login_key == "default"


def test_portfolio_start_request_accepts_real_login_key() -> None:
    """Sanity check: real keys are accepted unchanged."""
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="DUP733214",
        paper_trading=True,
        ib_login_key="marin1016test",
    )
    assert req.ib_login_key == "marin1016test"


def test_portfolio_start_request_strips_account_id_whitespace() -> None:
    """Codex iter 16 P2: leading/trailing whitespace on ``account_id``
    is silently stripped so the halt-latch key written by
    ``/drain/{account_id}`` (URL-stripped) always matches the key read
    by the supervisor's per-account halt check."""
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(),
        account_id="  DUP733214  ",
        paper_trading=True,
        ib_login_key="marin1016test",
    )
    assert req.account_id == "DUP733214"


def test_portfolio_start_request_rejects_internal_whitespace_account_id() -> None:
    """Codex iter 16 P2: internal whitespace (which can't be stripped)
    is rejected outright — IB account ids are alphanumeric."""
    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            account_id="DUP 733214",
            paper_trading=True,
            ib_login_key="marin1016test",
        )


def test_portfolio_start_request_rejects_empty_account_id() -> None:
    """Codex iter 16 P2: whitespace-only ``account_id`` is rejected
    (can't index halt latches on an empty key)."""
    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(),
            account_id="   ",
            paper_trading=True,
            ib_login_key="marin1016test",
        )
