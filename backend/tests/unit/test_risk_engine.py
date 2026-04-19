"""Unit tests for the pre-trade risk engine."""

from __future__ import annotations

import pytest

from msai.core.logging import setup_logging
from msai.services.risk_engine import RiskEngine, RiskLimits


class TestCheckPositionLimit:
    """Tests for ``RiskEngine.check_position_limit``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_check_position_limit_allows_normal(self) -> None:
        """Position within limit returns True."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_position_size=100.0))

        # Act
        result = engine.check_position_limit("AAPL", 50.0)

        # Assert
        assert result is True

    def test_check_position_limit_allows_exact_limit(self) -> None:
        """Position exactly at limit returns True."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_position_size=100.0))

        # Act
        result = engine.check_position_limit("AAPL", 100.0)

        # Assert
        assert result is True

    def test_check_position_limit_rejects_oversized(self) -> None:
        """Position over limit returns False."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_position_size=100.0))

        # Act
        result = engine.check_position_limit("AAPL", 150.0)

        # Assert
        assert result is False

    def test_check_position_limit_checks_absolute_value(self) -> None:
        """Negative quantities are checked by absolute value."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_position_size=100.0))

        # Act
        result = engine.check_position_limit("AAPL", -150.0)

        # Assert
        assert result is False

    def test_check_position_limit_rejects_when_halted(self) -> None:
        """When halted, all position checks return False."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_position_size=100.0))
        engine.kill_all()

        # Act
        result = engine.check_position_limit("AAPL", 10.0)

        # Assert
        assert result is False


class TestCheckDailyLoss:
    """Tests for ``RiskEngine.check_daily_loss``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_check_daily_loss_allows_positive_pnl(self) -> None:
        """Positive P&L returns True."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_daily_loss=-5000.0))

        # Act
        result = engine.check_daily_loss(1000.0)

        # Assert
        assert result is True
        assert engine.is_halted is False

    def test_check_daily_loss_halts_on_breach(self) -> None:
        """When P&L breaches limit, engine halts and returns False."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_daily_loss=-5000.0))

        # Act
        result = engine.check_daily_loss(-6000.0)

        # Assert
        assert result is False
        assert engine.is_halted is True

    def test_check_daily_loss_halts_on_exact_limit(self) -> None:
        """P&L exactly at the limit also triggers halt."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_daily_loss=-5000.0))

        # Act
        result = engine.check_daily_loss(-5000.0)

        # Assert
        assert result is False
        assert engine.is_halted is True


class TestCheckNotionalExposure:
    """Tests for ``RiskEngine.check_notional_exposure``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_check_notional_within_limit(self) -> None:
        """Notional within limit returns True."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_notional_exposure=500_000.0))

        # Act
        result = engine.check_notional_exposure(300_000.0)

        # Assert
        assert result is True

    def test_check_notional_over_limit(self) -> None:
        """Notional over limit returns False."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_notional_exposure=500_000.0))

        # Act
        result = engine.check_notional_exposure(600_000.0)

        # Assert
        assert result is False


class TestValidateDeployment:
    """Tests for ``RiskEngine.validate_deployment``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_validate_deployment_allows_within_limits(self) -> None:
        """Valid deployment within limits is allowed."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_concurrent_strategies=5))

        # Act
        allowed, reason = engine.validate_deployment({}, 3)

        # Assert
        assert allowed is True
        assert reason == "OK"

    def test_validate_deployment_rejects_when_halted(self) -> None:
        """Halted engine rejects all new deployments."""
        # Arrange
        engine = RiskEngine()
        engine.kill_all()

        # Act
        allowed, reason = engine.validate_deployment({}, 0)

        # Assert
        assert allowed is False
        assert "halted" in reason.lower()

    def test_validate_deployment_rejects_at_max_concurrent(self) -> None:
        """Rejects when already at maximum concurrent strategies."""
        # Arrange
        engine = RiskEngine(RiskLimits(max_concurrent_strategies=3))

        # Act
        allowed, reason = engine.validate_deployment({}, 3)

        # Assert
        assert allowed is False
        assert "concurrent" in reason.lower()


class TestKillAll:
    """Tests for ``RiskEngine.kill_all``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_kill_all_halts(self) -> None:
        """kill_all sets the halted flag."""
        # Arrange
        engine = RiskEngine()
        assert engine.is_halted is False

        # Act
        engine.kill_all()

        # Assert
        assert engine.is_halted is True


class TestReset:
    """Tests for ``RiskEngine.reset``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_reset_clears_halt(self) -> None:
        """reset clears the halted flag and daily P&L."""
        # Arrange
        engine = RiskEngine()
        engine.kill_all()
        assert engine.is_halted is True

        # Act
        engine.reset()

        # Assert
        assert engine.is_halted is False
