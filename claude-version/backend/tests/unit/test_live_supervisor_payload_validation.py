"""Proves the supervisor's payload factory calls the shared IB port
validators with the DEPLOYMENT ROW's account_id — NOT the process-wide
settings.ib_account_id.

Plan-review iter 2 caught a too-weak test that only exercised the
validators themselves; this version asserts the call site in
_build_production_payload_factory binds the right argument.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_payload_factory_validates_with_deployment_account_not_settings() -> None:
    """When IB_PORT=4001 (live) and the deployment row's account_id is
    a paper 'DU*' account, the factory must RAISE — regardless of what
    settings.ib_account_id says. This catches the iter-1 regression
    where the factory would have validated settings.ib_account_id
    instead of deployment.account_id.
    """
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    # Mock deployment row: paper account on LIVE port → must raise
    mock_deployment = MagicMock()
    mock_deployment.paper_trading = False  # matches IB_PORT=4001 on port side
    mock_deployment.account_id = "DU1234567"  # paper account — MISMATCH

    # Session returns the mock deployment
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=mock_deployment),
        ),
    )

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    session_factory = MagicMock(return_value=mock_session_ctx)

    # Settings with DIFFERENT ib_account_id from deployment's — proves
    # the factory is using the deployment row, not settings.
    with patch("msai.live_supervisor.__main__.settings") as mock_settings:
        mock_settings.ib_port = 4001  # live port
        mock_settings.ib_account_id = "U9999999"  # live account (matches port)
        # ^ If the factory validated settings.ib_account_id instead of
        #   deployment.account_id, this would PASS (4001 + U9999999 is
        #   valid). The mismatch must surface from deployment.account_id.

        factory = _build_production_payload_factory(session_factory)

        with pytest.raises(ValueError, match=r"DU1234567|paper"):
            await factory(
                row_id=uuid4(),
                deployment_id=uuid4(),
                deployment_slug="test-slug",
                payload_dict={},
            )


@pytest.mark.asyncio
async def test_payload_factory_validates_paper_trading_vs_port() -> None:
    """Second half of gotcha #6: deployment.paper_trading must match
    port even if account_id is consistent with port."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    mock_deployment = MagicMock()
    mock_deployment.paper_trading = True  # operator said 'paper'
    mock_deployment.account_id = "DU1234567"  # paper account — consistent

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=mock_deployment),
        ),
    )
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=mock_session_ctx)

    with patch("msai.live_supervisor.__main__.settings") as mock_settings:
        mock_settings.ib_port = 4001  # LIVE port — conflicts with paper_trading=True
        mock_settings.ib_account_id = "DU1234567"

        factory = _build_production_payload_factory(session_factory)

        with pytest.raises(ValueError, match="paper_trading=True"):
            await factory(
                row_id=uuid4(),
                deployment_id=uuid4(),
                deployment_slug="test-slug",
                payload_dict={},
            )
