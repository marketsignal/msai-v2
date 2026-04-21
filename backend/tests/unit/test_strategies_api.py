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
