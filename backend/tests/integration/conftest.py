"""Integration-test conftest -- re-exports shared portfolio-backtest fixtures.

Existing per-module Postgres fixtures (``isolated_postgres_url`` declared
inline in test files like ``test_portfolio_service.py``,
``test_portfolio_job_orchestration.py``, ``test_portfolio_full_lifecycle.py``)
keep working unchanged because pytest resolves fixtures locally first.
This file only adds NEW fixtures (portfolio_postgres_url,
portfolio_session_factory, portfolio_db_session, api_client_authed,
make_strategy, make_portfolio_with_strategies) that the portfolio-backtest
F1/F1c/F3/F4 tests use.

The canonical fixture source lives in ``conftest_portfolio_backtest.py``
(non-conftest filename) so pytest's auto-discovery does not pick it up
at the parent level and cross-pollute other test families.
"""

from __future__ import annotations

from tests.integration.conftest_portfolio_backtest import (  # noqa: F401
    _seed_user,
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
    make_portfolio_run,
    make_portfolio_with_strategies,
    make_strategy,
    portfolio_db_session,
    portfolio_postgres_url,
    portfolio_session_factory,
)
