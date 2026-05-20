"""Refactor contract: the new portfolio package must export the same public symbols."""

import importlib


def test_portfolio_package_exports_service() -> None:
    """PortfolioService is importable from the new package."""
    pkg = importlib.import_module("msai.services.portfolio")
    assert hasattr(pkg, "PortfolioService"), "PortfolioService must be re-exported"


def test_portfolio_package_exports_errors() -> None:
    pkg = importlib.import_module("msai.services.portfolio")
    assert hasattr(pkg, "PortfolioOrchestrationError")
    assert hasattr(pkg, "PortfolioRunTerminalStateError")


def test_legacy_shim_path_is_gone() -> None:
    """Task A4 deleted ``msai.services.portfolio_service``; every caller has
    been swept onto ``msai.services.portfolio.*``.  Importing the legacy
    path must now raise ``ModuleNotFoundError`` — this protects against
    accidental resurrection of the shim during later refactors.
    """
    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("msai.services.portfolio_service")
