"""Re-export shared fixtures for tests in this directory.

The canonical fixture modules live at
``tests/integration/conftest_symbol_onboarding.py`` and
``tests/integration/conftest_portfolio_backtest.py`` (named so that
pytest does NOT auto-discover them as conftest at the parent level).
API-level integration tests opt in by importing those fixtures through
this directory-local ``conftest.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

import pytest_asyncio

from tests.integration.conftest_portfolio_backtest import (  # noqa: F401
    _seed_user,
    api_client_authed,
    make_portfolio_with_strategies,
    portfolio_db_session,
    portfolio_postgres_url,
    portfolio_session_factory,
)
from tests.integration.conftest_symbol_onboarding import (  # noqa: F401
    isolated_postgres_url,
    mock_databento,
    mock_ib_refresh,
    session_factory,
)

if TYPE_CHECKING:
    from msai.models.portfolio import Portfolio


@pytest_asyncio.fixture
async def sample_portfolio_id(
    make_portfolio_with_strategies: Callable[..., Awaitable[Portfolio]],
) -> UUID:
    """A persisted Portfolio's ``id`` for tests that only need a target row.

    Reuses ``make_portfolio_with_strategies`` (the canonical portfolio
    factory) so the seed shape matches every other portfolio integration
    test (2 paper-candidate strategies + equal-weight allocations).
    """
    portfolio = await make_portfolio_with_strategies(n=2)
    return portfolio.id
