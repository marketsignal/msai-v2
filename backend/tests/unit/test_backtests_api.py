"""Unit tests for the backtests API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock AsyncSession that returns empty results by default."""
    session = AsyncMock(spec=AsyncSession)

    # Mock execute to return a result with scalars().all() -> empty list
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    # For func.count() queries -- scalar_one returns 0
    mock_result.scalar_one.return_value = 0
    mock_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_result
    return session


@pytest.fixture
def client_with_mock_db(mock_db: AsyncMock) -> httpx.AsyncClient:
    """Async test client with the DB dependency overridden to use a mock."""

    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    app.dependency_overrides[get_db] = _override_get_db

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield client  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/backtests/history
# ---------------------------------------------------------------------------


class TestListBacktests:
    """Tests for GET /api/v1/backtests/history."""

    async def test_list_backtests_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/backtests/history returns 200 with paginated results."""
        response = await client_with_mock_db.get("/api/v1/backtests/history")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert isinstance(body["items"], list)
        assert body["total"] == 0

    async def test_list_backtests_accepts_pagination_params(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/backtests/history accepts page and page_size params."""
        response = await client_with_mock_db.get("/api/v1/backtests/history?page=2&page_size=10")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_list_backtests_rejects_invalid_page(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/backtests/history rejects page < 1."""
        response = await client_with_mock_db.get("/api/v1/backtests/history?page=0")

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tests: server-authoritative config validation helper
#
# Covers the Hawk council blocking objection #4 (2026-04-20): every entry
# point (API/CLI/UI) must hit the same validation surface, not only the UI.
# The helper itself is unit-testable without spinning up Redis / Databento.
# ---------------------------------------------------------------------------


class TestPrepareAndValidateBacktestConfig:
    """Tests for :func:`msai.api.backtests._prepare_and_validate_backtest_config`."""

    @staticmethod
    def _example_strategy() -> tuple[str, str]:
        """Return ``(file_path, config_class_name)`` for the example strategy.

        ``strategies/`` lives at the repo root, not inside ``backend/`` —
        same convention as ``tests/unit/test_strategy_registry.py:22``.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "strategies" / "example"
        return (str(root / "ema_cross.py"), "EMACrossConfig")

    def test_accepts_well_formed_config_and_injects_instruments(self) -> None:
        """Config missing ``instrument_id``/``bar_type`` gets injected from
        resolved instruments + round-trips through StrategyConfig.parse."""
        from msai.api.backtests import _prepare_and_validate_backtest_config

        file_path, config_class = self._example_strategy()
        config = {"fast_ema_period": 5, "slow_ema_period": 20}

        prepared = _prepare_and_validate_backtest_config(
            config,
            strategy_file_path=file_path,
            config_class_name=config_class,
            canonical_instruments=["AAPL.NASDAQ"],
        )

        # Caller's dict is untouched
        assert config == {"fast_ema_period": 5, "slow_ema_period": 20}
        # Injection happened
        assert prepared["instrument_id"] == "AAPL.NASDAQ"
        assert prepared["bar_type"] == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
        # Caller-supplied values preserved
        assert prepared["fast_ema_period"] == 5

    def test_rejects_malformed_instrument_id_with_422_and_field_path(
        self,
    ) -> None:
        """Malformed ``instrument_id`` → raises ``StrategyConfigValidationError``
        with ``envelope()`` matching the api-design.md shape: a top-level
        ``{"error": {"code", "message", "details": [{"field", "message"}]}}``.
        The FastAPI exception handler at ``main.py::_strategy_config_validation_handler``
        converts the raise into the 422 JSON response. Frontend consumes
        ``error.details[0].field`` to highlight the bad input."""
        from msai.api.backtests import (
            StrategyConfigValidationError,
            _prepare_and_validate_backtest_config,
        )

        file_path, config_class = self._example_strategy()

        with pytest.raises(StrategyConfigValidationError) as excinfo:
            _prepare_and_validate_backtest_config(
                {"instrument_id": "garbage"},
                strategy_file_path=file_path,
                config_class_name=config_class,
                canonical_instruments=["AAPL.NASDAQ"],
            )

        envelope = excinfo.value.envelope()["error"]
        assert envelope["code"] == "VALIDATION_ERROR"
        # Field must be the PLAIN dotted path — no backticks, no $. prefix
        # — so frontend fieldErrors[name] lookup matches schema keys.
        assert envelope["details"][0]["field"] == "instrument_id"

    def test_skips_validation_gracefully_when_no_config_class(self, tmp_path: Path) -> None:
        """Legacy strategies without a matching ``*Config`` class don't
        block the run path — worker still catches bad payloads downstream.
        The API passes ``config_class_name=None`` for those strategies."""

        from msai.api.backtests import _prepare_and_validate_backtest_config

        # Path doesn't matter — the None path short-circuits before load.
        bogus = tmp_path / "no_such_strategy.py"
        bogus.write_text("# empty\n", encoding="utf-8")

        prepared = _prepare_and_validate_backtest_config(
            {"anything": 1},
            strategy_file_path=str(bogus),
            config_class_name=None,
            canonical_instruments=[],
        )

        # Returns the config unchanged (no canonical_instruments → no inject)
        assert prepared == {"anything": 1}

    def test_rejects_config_class_that_uses_nonstandard_naming(self) -> None:
        """Regression for Codex code-review P1 2026-04-21: the helper
        MUST accept an arbitrary config class name (not just
        ``FooConfig`` derived from ``FooStrategy``). The discovered
        class name flows in via ``DiscoveredStrategy.config_class_name``
        which persistence stores on ``Strategy.config_class``. Validates
        that a config class whose name DOESN'T match ``<strategy>Config``
        still gets server-authoritative validation."""
        from msai.api.backtests import (
            StrategyConfigValidationError,
            _prepare_and_validate_backtest_config,
        )

        file_path, config_class = self._example_strategy()
        # The signature-level regression is that the test now
        # EXPLICITLY passes the discovered name, so any future rename
        # (e.g. ``EMACrossParams``) would flow through without a code
        # change to the helper.
        with pytest.raises(StrategyConfigValidationError):
            _prepare_and_validate_backtest_config(
                {"instrument_id": "bad"},
                strategy_file_path=file_path,
                config_class_name=config_class,
                canonical_instruments=["AAPL.NASDAQ"],
            )
