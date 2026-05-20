"""Refactor contract for Task A2: CRUD lives on PortfolioLifecycle.

Per Task A2 of ``docs/plans/portfolio-backtest.md``, the CRUD methods that
were colocated with :class:`PortfolioService` in ``orchestration.py`` get
extracted to a new module ``msai.services.portfolio.lifecycle`` and exposed
as ``@staticmethod`` members of :class:`PortfolioLifecycle`.

The actual method names on this repo's ``PortfolioService`` use the short
form (``create`` / ``list`` / ``get``), not the verbose form suggested in
the plan template (``create_portfolio`` / ``list_portfolios`` /
``get_portfolio``). The plan explicitly allows the implementer to use
what's actually there ("If the method names in your repo differ from this
list, use what's actually there"), so this test asserts the short form
that matches today's public API.
"""

from __future__ import annotations


def test_lifecycle_module_exposes_class() -> None:
    """``PortfolioLifecycle`` is exported from ``portfolio.lifecycle``."""
    from msai.services.portfolio import lifecycle

    assert hasattr(lifecycle, "PortfolioLifecycle")


def test_lifecycle_holds_crud_static_methods() -> None:
    """Every CRUD/lifecycle method moved off PortfolioService lives here."""
    from msai.services.portfolio.lifecycle import PortfolioLifecycle

    expected = [
        "create",
        "list",
        "get",
        "get_allocations",
        "create_run",
        "list_runs",
        "get_run",
        "count",
        "count_runs",
        "mark_run_running",
        "heartbeat_run",
        "mark_run_failed",
    ]
    for name in expected:
        attr = getattr(PortfolioLifecycle, name, None)
        assert callable(attr), f"PortfolioLifecycle.{name} must be a callable static method"


def test_orchestration_delegates_to_lifecycle() -> None:
    """``PortfolioService`` keeps the same public CRUD methods (back-compat)."""
    from msai.services.portfolio import PortfolioService

    for name in (
        "create",
        "list",
        "get",
        "get_allocations",
        "create_run",
        "list_runs",
        "get_run",
        "count",
        "count_runs",
        "mark_run_running",
        "heartbeat_run",
        "mark_run_failed",
    ):
        assert callable(getattr(PortfolioService, name, None)), (
            f"PortfolioService.{name} must remain callable for back-compat"
        )


def test_orchestration_keeps_run_engine() -> None:
    """The actual run-the-engine method stays on PortfolioService."""
    from msai.services.portfolio import PortfolioService

    assert callable(getattr(PortfolioService, "run_portfolio_backtest", None))
