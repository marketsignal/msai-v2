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

    def test_bar_type_preserves_user_step_and_aggregation(self) -> None:
        """Bar-type rewrite must preserve caller-selected
        step/aggregation/price_type/source — only the instrument prefix
        is replaced with the canonical form.

        Codex P1 catch (PR #61 round 4): an earlier unconditional
        ``bar_type = f"{canonical_id}-1-MINUTE-LAST-EXTERNAL"`` silently
        coerced 5-minute, hourly, BID/ASK, INTERNAL-source bar types
        back to the 1-minute LAST EXTERNAL default. Callers who
        legitimately picked non-default specs got a different backtest
        than they asked for.
        """
        from msai.api.backtests import _prepare_and_validate_backtest_config

        file_path, config_class = self._example_strategy()
        # User submits MIC venue + non-default 5-minute bar spec.
        config = {
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-5-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
        }
        prepared = _prepare_and_validate_backtest_config(
            config,
            strategy_file_path=file_path,
            config_class_name=config_class,
            canonical_instruments=["AAPL.NASDAQ"],
        )

        # Instrument prefix rewritten to canonical; step/aggregation/
        # price_type/source preserved verbatim.
        assert prepared["instrument_id"] == "AAPL.NASDAQ"
        assert prepared["bar_type"] == "AAPL.NASDAQ-5-MINUTE-LAST-EXTERNAL"

    def test_bar_type_preserves_user_bid_price_type(self) -> None:
        """Another bar_type spec preservation case: BID price_type +
        INTERNAL source (a synthetic-bar setup) survives the rewrite.
        """
        from msai.api.backtests import _prepare_and_validate_backtest_config

        file_path, config_class = self._example_strategy()
        config = {
            "instrument_id": "SPY.NASDAQ",
            "bar_type": "SPY.NASDAQ-15-MINUTE-BID-INTERNAL",
        }
        prepared = _prepare_and_validate_backtest_config(
            config,
            strategy_file_path=file_path,
            config_class_name=config_class,
            canonical_instruments=["SPY.ARCA"],
        )
        assert prepared["instrument_id"] == "SPY.ARCA"
        assert prepared["bar_type"] == "SPY.ARCA-15-MINUTE-BID-INTERNAL"

    def test_overwrites_user_supplied_instrument_id_with_canonical(self) -> None:
        """Caller-supplied ``instrument_id`` MUST be overwritten by the
        canonical form returned by the resolver — not just "injected if
        missing".

        The 2026-05-12 data-path closure made the read-boundary resolver
        accept both Databento MIC (``AAPL.XNAS``) and exchange-name
        (``AAPL.NASDAQ``) input. The resolver canonicalizes either form to
        the registry's exchange-name canonical. If the API layer leaves
        the caller's input form in ``config.instrument_id`` while
        ``Backtest.instruments`` gets the canonical form, the Nautilus
        subprocess reads the catalog at one path while the writer landed
        bars at another → ``trade_count=0``. Surfaced on the prod AAPL
        backtest 2026-05-12.
        """
        from msai.api.backtests import _prepare_and_validate_backtest_config

        file_path, config_class = self._example_strategy()
        # Caller submits the MIC form (what ``msai ingest stocks`` prints).
        config = {
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
        }
        # Resolver returns the canonical exchange-name form.
        prepared = _prepare_and_validate_backtest_config(
            config,
            strategy_file_path=file_path,
            config_class_name=config_class,
            canonical_instruments=["AAPL.NASDAQ"],
        )

        # The user-input form is REPLACED with the canonical form.
        assert prepared["instrument_id"] == "AAPL.NASDAQ"
        assert prepared["bar_type"] == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
        # Other caller-supplied fields preserved.
        assert prepared["fast_ema_period"] == 10
        assert prepared["slow_ema_period"] == 30

    def test_rejects_malformed_field_with_422_and_field_path(
        self,
    ) -> None:
        """Malformed strategy-config field → raises ``StrategyConfigValidationError``
        with ``envelope()`` matching the api-design.md shape: a top-level
        ``{"error": {"code", "message", "details": [{"field", "message"}]}}``.
        The FastAPI exception handler at ``main.py::_strategy_config_validation_handler``
        converts the raise into the 422 JSON response. Frontend consumes
        ``error.details[0].field`` to highlight the bad input.

        Note: the canonical_instruments **overwrite** ``instrument_id`` /
        ``bar_type`` per the 2026-05-12 data-path closure (preventing
        catalog-key mismatch when user submits MIC form but registry stores
        exchange-name form). To test the validation path, pin a different
        strategy-config field — ``fast_ema_period`` must be a positive int.
        """
        from msai.api.backtests import (
            StrategyConfigValidationError,
            _prepare_and_validate_backtest_config,
        )

        file_path, config_class = self._example_strategy()

        with pytest.raises(StrategyConfigValidationError) as excinfo:
            _prepare_and_validate_backtest_config(
                {"fast_ema_period": "not-an-int"},
                strategy_file_path=file_path,
                config_class_name=config_class,
                canonical_instruments=["AAPL.NASDAQ"],
            )

        envelope = excinfo.value.envelope()["error"]
        assert envelope["code"] == "VALIDATION_ERROR"
        # Field must be the PLAIN dotted path — no backticks, no $. prefix
        # — so frontend fieldErrors[name] lookup matches schema keys.
        assert envelope["details"][0]["field"] == "fast_ema_period"

    def test_canonical_overrides_malformed_user_instrument_id(self) -> None:
        """A malformed ``instrument_id`` in the user's submission is silently
        overwritten by the canonical from the resolver — not propagated to
        the validator. This is intentional: the API canonicalization step
        guarantees a valid form before the strategy-config validator runs.

        Prior to the 2026-05-12 fix this branch was unreachable (a
        malformed ``instrument_id`` would surface at validation as a
        per-field 422). After the fix, the canonical takes over and the
        validator only sees the registry-authoritative form. The user's
        original malformed value is preserved nowhere — that's the contract.
        """
        from msai.api.backtests import _prepare_and_validate_backtest_config

        file_path, config_class = self._example_strategy()
        prepared = _prepare_and_validate_backtest_config(
            {"instrument_id": "garbage"},
            strategy_file_path=file_path,
            config_class_name=config_class,
            canonical_instruments=["AAPL.NASDAQ"],
        )

        # Malformed input silently dropped; canonical takes its place.
        assert prepared["instrument_id"] == "AAPL.NASDAQ"
        assert prepared["bar_type"] == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"

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
        still gets server-authoritative validation.

        Use ``fast_ema_period`` as the malformed-field probe (instead of
        ``instrument_id``, which the 2026-05-12 canonicalization step now
        overrides before validation runs).
        """
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
                {"fast_ema_period": "not-an-int"},
                strategy_file_path=file_path,
                config_class_name=config_class,
                canonical_instruments=["AAPL.NASDAQ"],
            )


# ---------------------------------------------------------------------------
# Tests: failure envelope (Task B8, feature backtest-failure-surfacing)
# ---------------------------------------------------------------------------


class TestStatusEndpointReturnsErrorEnvelope:
    """Verify GET /api/v1/backtests/{id}/status surfaces the structured
    ErrorEnvelope for failed rows, null for non-failed, and sanitized
    raw message for historical (pre-migration) rows with NULL public_message.
    """

    async def test_failed_row_returns_structured_envelope(
        self, client, seed_failed_backtest
    ) -> None:
        bt_id, _raw_msg = seed_failed_backtest
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] is not None
        assert body["error"]["code"] == "missing_data"
        assert body["error"]["message"]
        assert body["error"]["suggested_action"]
        assert body["error"]["remediation"]["kind"] == "ingest_data"

    async def test_pending_row_has_no_error_field(self, client, seed_pending_backtest) -> None:
        bt_id = seed_pending_backtest
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        assert response.status_code == 200
        body = response.json()
        # [Phase 5 P2] PRD contract: ``error`` is ABSENT (not null) on
        # non-failed rows. ``response_model_exclude_none=True`` enforces.
        assert "error" not in body

    async def test_historical_row_degrades_to_unknown(
        self, client, seed_historical_failed_row
    ) -> None:
        """US-006: post-migration, historical failed rows have
        error_code='unknown' (server_default), error_public_message=NULL,
        and their raw error_message populated. The API must surface
        the stored message through error.message (sanitized-on-read) —
        never a blank envelope."""
        bt_id = seed_historical_failed_row
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        body = response.json()
        assert body["error"]["code"] == "unknown"
        assert body["error"]["message"]  # stored raw message surfaces (sanitized)
        # Sanitizer must strip /app/ paths even on read.
        assert "/app/" not in body["error"]["message"]


class TestHistoryEndpointReturnsCompactError:
    """Verify GET /api/v1/backtests/history items expose error_code +
    error_public_message on failed rows (compact, no suggested_action /
    remediation — those live only on the detail endpoint).
    """

    async def test_failed_rows_include_error_code_and_message(
        self, client, seed_failed_backtest
    ) -> None:
        response = await client.get("/api/v1/backtests/history")
        assert response.status_code == 200
        items = response.json()["items"]
        failed = [i for i in items if i["status"] == "failed"]
        assert failed, "fixture must seed at least one failed row"
        f = failed[0]
        assert "error_code" in f
        assert "error_public_message" in f
        # History is compact — no suggested_action / remediation here.
        assert "suggested_action" not in f
        assert "remediation" not in f
