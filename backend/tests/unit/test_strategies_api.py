"""Unit tests for the strategies API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest

from msai.core.database import get_db
from msai.main import app
from msai.models.strategy import Strategy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies" / "example"


class _FakeSession:
    """Minimal async DB session for unit tests.

    Supports the tiny subset of the SQLAlchemy async API used by the
    strategies endpoint: ``execute(select(...))`` → result with ``scalars()``,
    ``add``, ``commit``, ``refresh``. The fake records every added row
    and returns them on subsequent scalar queries.
    """

    def __init__(self) -> None:
        self._rows: list[Strategy] = []

    async def execute(self, _stmt: object) -> _FakeSession:
        return self

    def scalars(self) -> _FakeSession:
        return self

    def all(self) -> list[Strategy]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Strategy | None:
        return self._rows[0] if self._rows else None

    def add(self, row: Strategy) -> None:
        if row.id is None:
            row.id = uuid4()
        if row.created_at is None:
            row.created_at = datetime.now(UTC)
        self._rows.append(row)

    async def commit(self) -> None:
        pass

    async def refresh(self, _row: Strategy) -> None:
        pass

    async def delete(self, row: Strategy) -> None:
        if row in self._rows:
            self._rows.remove(row)


@pytest.fixture
def fake_db_session() -> _FakeSession:
    """Return a fake DB session and install it as the get_db override."""
    session = _FakeSession()

    async def _override() -> AsyncGenerator[_FakeSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    yield session
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client(fake_db_session: _FakeSession) -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application with fake DB."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/strategies/
# ---------------------------------------------------------------------------


class TestListStrategies:
    """Tests for GET /api/v1/strategies/."""

    async def test_list_strategies_returns_200(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/strategies/ returns 200 with a list of strategies."""
        response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert isinstance(body["items"], list)
        assert isinstance(body["total"], int)

    async def test_list_strategies_discovers_example(self, client: httpx.AsyncClient) -> None:
        """GET /api/v1/strategies/ discovers the example EMA cross strategy."""
        # Patch _STRATEGIES_DIR to point at the example strategies
        with patch("msai.api.strategies._STRATEGIES_DIR", STRATEGIES_DIR):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 1

        class_names = [item["strategy_class"] for item in body["items"]]
        assert "EMACrossStrategy" in class_names

    async def test_list_strategies_empty_dir(
        self, client: httpx.AsyncClient, tmp_path: Path
    ) -> None:
        """GET /api/v1/strategies/ returns empty list for empty directory."""
        empty_dir = tmp_path / "empty_strategies"
        empty_dir.mkdir()

        with patch("msai.api.strategies._STRATEGIES_DIR", empty_dir):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []

    async def test_list_strategies_surfaces_config_schema_status(
        self, client: httpx.AsyncClient
    ) -> None:
        """Each strategy row carries a ``config_schema_status`` so the
        frontend can distinguish ready-for-form-render from degraded
        cases (council blocking objection Hawk #3, Maintainer #1)."""
        with patch("msai.api.strategies._STRATEGIES_DIR", STRATEGIES_DIR):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        for item in body["items"]:
            assert "config_schema_status" in item
            assert item["config_schema_status"] in {
                "ready",
                "unsupported",
                "extraction_failed",
                "no_config_class",
            }

    async def test_list_strategies_ema_cross_exposes_ready_schema(
        self, client: httpx.AsyncClient
    ) -> None:
        """EMACrossStrategy extracts cleanly via msgspec schema_hook —
        status=ready + user-field schema populated + inherited base
        fields trimmed (council acceptance criterion #1)."""
        with patch("msai.api.strategies._STRATEGIES_DIR", STRATEGIES_DIR):
            response = await client.get("/api/v1/strategies/")

        assert response.status_code == 200
        body = response.json()
        ema = next(
            (i for i in body["items"] if i["strategy_class"] == "EMACrossStrategy"),
            None,
        )
        assert ema is not None
        assert ema["config_schema_status"] == "ready"
        schema = ema["config_schema"]
        assert schema is not None
        assert schema["type"] == "object"
        # User fields present, inherited plumbing absent
        assert "fast_ema_period" in schema["properties"]
        assert "instrument_id" in schema["properties"]
        assert "manage_stop" not in schema["properties"]  # inherited — trimmed
        assert "order_id_tag" not in schema["properties"]  # inherited — trimmed
        # Defaults populated
        defaults = ema["default_config"]
        assert defaults is not None
        assert defaults["fast_ema_period"] == 10
        assert defaults["slow_ema_period"] == 30


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/strategies/{id}/validate
# ---------------------------------------------------------------------------


class TestValidateStrategy:
    """Tests for POST /api/v1/strategies/{id}/validate."""

    async def test_validate_strategy_returns_200(
        self,
        client: httpx.AsyncClient,
        fake_db_session: _FakeSession,
    ) -> None:
        """POST /api/v1/strategies/{id}/validate returns 200 for a valid strategy."""
        # Arrange: seed a real Strategy row pointing at the example EMA file.
        strategy = Strategy(
            name="example.ema_cross",
            description="EMA Cross",
            file_path=str(STRATEGIES_DIR / "ema_cross.py"),
            strategy_class="EMACrossStrategy",
            config_schema=None,
            default_config=None,
        )
        fake_db_session.add(strategy)
        assert strategy.id is not None

        # Act
        response = await client.post(f"/api/v1/strategies/{strategy.id}/validate")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert "message" in body
        assert "validated successfully" in body["message"]

    async def test_validate_strategy_missing_row_returns_404(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """POST /validate returns 404 when the strategy row does not exist."""
        # Arrange: empty session -> scalar_one_or_none() returns None
        strategy_id = UUID(int=0)

        # Act
        response = await client.post(f"/api/v1/strategies/{strategy_id}/validate")

        # Assert
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: PATCH /api/v1/strategies/{id} rejects dispatch-time fields
# ---------------------------------------------------------------------------


class TestPatchStrategyRejectsManagedKeys:
    """The PATCH endpoint must reject ``default_config`` payloads that try to
    set ``instrument_id`` or ``bar_type``.

    These are dispatch-time fields injected by the portfolio orchestrator at
    runtime from the candidate's ``instruments`` list plus the platform's
    canonical bar_type. Setting them on ``strategy.default_config`` silently
    snapshots into every GraduationCandidate.config created during the
    buggy window (per ``services/portfolio/orchestration.py:1062`` merge
    order ``{**default_config, **candidate.config}`` puts candidate ON TOP).
    Reverting the default_config doesn't help — candidate snapshots persist.

    Repro chain: 2026-05-20 night EMA Cross backtest produced 0 events for
    4 hours until the operator psql-edited the poisoned candidate. See
    ``memory/feedback_candidate_config_snapshots_stale_default_config.md``.
    """

    def _make_strategy(self) -> Strategy:
        return Strategy(
            id=uuid4(),
            name="example.ema_cross",
            description="EMA Cross",
            file_path="/app/strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            config_class="EMACrossConfig",
            config_schema={
                "type": "object",
                "properties": {
                    "instrument_id": {"type": "string"},
                    "bar_type": {"type": "string"},
                    "fast_ema_period": {"type": "integer", "default": 10},
                },
            },
            default_config={"fast_ema_period": 10},
            config_schema_status="ready",
            code_hash="hash",
            created_at=datetime.now(UTC),
        )

    async def test_patch_with_instrument_id_in_default_config_returns_422(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """PATCH with default_config containing instrument_id is rejected."""
        # Arrange
        strategy = self._make_strategy()
        fake_db_session._rows.append(strategy)

        # Act
        response = await client.patch(
            f"/api/v1/strategies/{strategy.id}",
            json={"default_config": {"instrument_id": "AAPL.NASDAQ"}},
        )

        # Assert
        assert response.status_code == 422
        body = response.json()
        detail = body.get("detail", body)
        error = detail.get("error", detail)
        assert error.get("code") == "MANAGED_CONFIG_KEY"
        rejected = error.get("details", [{}])[0].get("rejected_keys", [])
        assert "instrument_id" in rejected
        # Strategy state unchanged
        assert strategy.default_config == {"fast_ema_period": 10}

    async def test_patch_with_bar_type_in_default_config_returns_422(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """PATCH with default_config containing bar_type is rejected."""
        # Arrange
        strategy = self._make_strategy()
        fake_db_session._rows.append(strategy)

        # Act
        response = await client.patch(
            f"/api/v1/strategies/{strategy.id}",
            json={
                "default_config": {"bar_type": "AAPL.NASDAQ-1-DAY-LAST-EXTERNAL"}
            },
        )

        # Assert
        assert response.status_code == 422
        body = response.json()
        detail = body.get("detail", body)
        error = detail.get("error", detail)
        assert error.get("code") == "MANAGED_CONFIG_KEY"
        rejected = error.get("details", [{}])[0].get("rejected_keys", [])
        assert "bar_type" in rejected
        # Strategy state unchanged
        assert strategy.default_config == {"fast_ema_period": 10}

    async def test_patch_with_both_managed_keys_lists_both_in_error(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """PATCH with both managed keys returns 422 listing both in the error."""
        # Arrange
        strategy = self._make_strategy()
        fake_db_session._rows.append(strategy)

        # Act
        response = await client.patch(
            f"/api/v1/strategies/{strategy.id}",
            json={
                "default_config": {
                    "instrument_id": "AAPL.NASDAQ",
                    "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                    "fast_ema_period": 20,
                }
            },
        )

        # Assert
        assert response.status_code == 422
        body = response.json()
        detail = body.get("detail", body)
        error = detail.get("error", detail)
        assert error.get("code") == "MANAGED_CONFIG_KEY"
        rejected = set(error.get("details", [{}])[0].get("rejected_keys", []))
        assert rejected == {"instrument_id", "bar_type"}

    async def test_patch_legitimate_field_still_works(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """PATCH with a non-managed default_config field still succeeds (control test)."""
        # Arrange
        strategy = self._make_strategy()
        fake_db_session._rows.append(strategy)

        # Act
        response = await client.patch(
            f"/api/v1/strategies/{strategy.id}",
            json={"default_config": {"fast_ema_period": 25}},
        )

        # Assert
        assert response.status_code == 200
        assert strategy.default_config == {"fast_ema_period": 25}

    async def test_patch_description_without_default_config_works(
        self, client: httpx.AsyncClient, fake_db_session: _FakeSession
    ) -> None:
        """PATCH that only touches description doesn't trigger the validator."""
        # Arrange
        strategy = self._make_strategy()
        fake_db_session._rows.append(strategy)

        # Act
        response = await client.patch(
            f"/api/v1/strategies/{strategy.id}",
            json={"description": "Updated description"},
        )

        # Assert
        assert response.status_code == 200
        assert strategy.description == "Updated description"
