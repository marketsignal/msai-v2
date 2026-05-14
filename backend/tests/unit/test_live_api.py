"""Unit tests for the live trading API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api.live_deps import get_command_bus
from msai.core.database import get_db
from msai.main import app
from msai.services.live_command_bus import LiveCommandBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock AsyncSession that returns empty results by default."""
    session = AsyncMock(spec=AsyncSession)

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one.return_value = 0
    mock_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_result
    return session


@pytest.fixture
def mock_command_bus() -> MagicMock:
    """Stub LiveCommandBus that records publishes and serves
    Redis-y methods needed by the kill-switch endpoint."""
    bus = MagicMock(spec=LiveCommandBus)
    bus.publish_stop = AsyncMock(return_value="1-0")
    # The kill_all endpoint pokes bus._redis directly for the
    # halt-flag SET/DELETE — provide a fake redis with the
    # async methods it calls.
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.delete = AsyncMock(return_value=1)
    fake_redis.exists = AsyncMock(return_value=0)
    bus._redis = fake_redis  # noqa: SLF001
    return bus


@pytest.fixture
def client_with_mock_db(
    mock_db: AsyncMock,
    mock_command_bus: MagicMock,
) -> httpx.AsyncClient:
    """Async test client with the DB and command bus
    dependencies overridden to use mocks."""

    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    async def _override_get_bus() -> LiveCommandBus:
        return mock_command_bus

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus

    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://testserver")  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/status
# ---------------------------------------------------------------------------


class TestLiveStatus:
    """Tests for GET /api/v1/live/status."""

    async def test_live_status_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/status returns 200 with deployment list."""
        response = await client_with_mock_db.get("/api/v1/live/status")

        assert response.status_code == 200
        body = response.json()
        assert "deployments" in body
        assert "risk_halted" in body
        assert "active_count" in body
        assert isinstance(body["deployments"], list)

    async def test_live_status_accepts_active_only_query_param(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """GET /api/v1/live/status?active_only=true returns 200.

        Slice 4 PR #58 Codex P1 fix: the deploy.yml active-deployments gate
        uses this query param to bypass the default 50-row cap so a
        long-running broker deployment can't be pushed off the response by
        50+ subsequent stop events.
        """
        response = await client_with_mock_db.get("/api/v1/live/status?active_only=true")

        assert response.status_code == 200
        body = response.json()
        assert "deployments" in body
        # Same response shape; only the filter + cap differs server-side.
        assert "risk_halted" in body
        assert "active_count" in body


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/live/kill-all
# ---------------------------------------------------------------------------


class TestLiveKillAll:
    """Tests for POST /api/v1/live/kill-all (Phase 3 task 3.9 —
    push-based kill switch with persistent halt flag)."""

    async def test_kill_all_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """POST /api/v1/live/kill-all returns 200 with stopped count."""
        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        assert response.status_code == 200
        body = response.json()
        assert "stopped" in body
        assert "risk_halted" in body
        assert isinstance(body["stopped"], int)

    async def test_kill_all_sets_persistent_halt_flag(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """Layer 1: ``msai:risk:halt`` must be SET on every
        kill-all so subsequent ``/start`` calls return 503."""
        await client_with_mock_db.post("/api/v1/live/kill-all")

        set_calls = mock_command_bus._redis.set.call_args_list  # noqa: SLF001
        halt_keys = [call.args[0] for call in set_calls]
        assert "msai:risk:halt" in halt_keys
        # 24h TTL applied
        for call in set_calls:
            if call.args[0] == "msai:risk:halt":
                assert call.kwargs.get("ex") == 86400

    async def test_kill_all_publishes_stop_for_each_active_row(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_db: AsyncMock,
        mock_command_bus: MagicMock,
    ) -> None:
        """Layer 3: a stop command must be published for every
        ``live_node_processes`` row in an active status."""
        from uuid import uuid4

        from msai.models.live_node_process import LiveNodeProcess

        rows = [
            LiveNodeProcess(deployment_id=uuid4(), status="running"),
            LiveNodeProcess(deployment_id=uuid4(), status="ready"),
            LiveNodeProcess(deployment_id=uuid4(), status="building"),
        ]
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = rows
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        assert response.status_code == 200
        body = response.json()
        assert body["stopped"] == 3
        assert mock_command_bus.publish_stop.await_count == 3
        # Each call carries the kill_switch reason
        for call in mock_command_bus.publish_stop.await_args_list:
            assert call.kwargs.get("reason") == "kill_switch"

    async def test_kill_all_continues_when_one_publish_fails(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_db: AsyncMock,
        mock_command_bus: MagicMock,
    ) -> None:
        """Codex batch 9 P1 regression: an emergency-stop
        endpoint MUST surface failures. If publishing a stop
        command fails for one row, the endpoint continues
        with the rest BUT returns 207 Multi-Status with the
        failure count so the operator sees there's an
        unstopped deployment requiring manual attention.
        Earlier code returned 200 with no failure indicator
        — a dangerous silent-failure mode."""
        from uuid import uuid4

        from msai.models.live_node_process import LiveNodeProcess

        rows = [
            LiveNodeProcess(deployment_id=uuid4(), status="running"),
            LiveNodeProcess(deployment_id=uuid4(), status="running"),
        ]
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = rows
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        # First call raises, second succeeds
        mock_command_bus.publish_stop.side_effect = [RuntimeError("redis blip"), "1-0"]

        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        # 207 Multi-Status: partial success — one stopped,
        # one failed
        assert response.status_code == 207
        body = response.json()
        assert body["stopped"] == 1
        assert body["failed_publish"] == 1
        # Halt flag was still set despite the publish error
        assert body["risk_halted"] is True

    async def test_kill_all_clean_path_includes_zero_failures(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """Clean kill-all returns 200 with failed_publish=0."""
        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        assert response.status_code == 200
        body = response.json()
        assert body["failed_publish"] == 0


class TestLiveResume:
    """Tests for POST /api/v1/live/resume (Phase 3 task 3.9 —
    clears the persistent halt flag)."""

    async def test_resume_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        response = await client_with_mock_db.post("/api/v1/live/resume")

        assert response.status_code == 200
        body = response.json()
        assert body["resumed"] is True

    async def test_resume_deletes_halt_flag(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """The endpoint must delete the persistent halt flag
        AND its metadata keys (set_by, set_at)."""
        await client_with_mock_db.post("/api/v1/live/resume")

        delete_calls = mock_command_bus._redis.delete.call_args_list  # noqa: SLF001
        deleted_keys = [call.args[0] for call in delete_calls]
        assert "msai:risk:halt" in deleted_keys
        assert "msai:risk:halt:set_by" in deleted_keys
        assert "msai:risk:halt:set_at" in deleted_keys


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/positions
# ---------------------------------------------------------------------------


class TestLivePositions:
    """Tests for GET /api/v1/live/positions."""

    async def test_live_positions_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/positions returns 200 with positions list."""
        response = await client_with_mock_db.get("/api/v1/live/positions")

        assert response.status_code == 200
        body = response.json()
        assert "positions" in body
        assert isinstance(body["positions"], list)


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/live/trades
# ---------------------------------------------------------------------------


class TestLiveTrades:
    """Tests for GET /api/v1/live/trades."""

    async def test_live_trades_returns_200(self, client_with_mock_db: httpx.AsyncClient) -> None:
        """GET /api/v1/live/trades returns 200 with trades list."""
        response = await client_with_mock_db.get("/api/v1/live/trades")

        assert response.status_code == 200
        body = response.json()
        assert "trades" in body
        assert "total" in body
        assert isinstance(body["trades"], list)
        assert body["total"] == 0

    async def test_live_trades_deployment_id_filter_applies_where_clause(
        self, client_with_mock_db: httpx.AsyncClient, mock_db: AsyncMock
    ) -> None:
        """Regression (multi-symbol drill 2026-04-20): passing
        ``?deployment_id=<uuid>`` must add a WHERE clause on
        ``OrderAttemptAudit.deployment_id`` so callers can scope to a
        single deployment instead of getting all live fills."""
        from uuid import uuid4

        dep_id = uuid4()
        response = await client_with_mock_db.get(f"/api/v1/live/trades?deployment_id={dep_id}")
        assert response.status_code == 200

        executed_sqls = [
            str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            for call in mock_db.execute.await_args_list
        ]
        # SQLAlchemy's literal_binds strips UUID hyphens when compiling
        # to a Postgres UUID literal; match the hexdigest form.
        dep_hex = dep_id.hex
        assert any(
            "deployment_id" in sql and (str(dep_id) in sql or dep_hex in sql)
            for sql in executed_sqls
        ), f"Expected a WHERE on deployment_id={dep_id!s}; got SQLs: {executed_sqls}"

    async def test_live_trades_rejects_malformed_deployment_id(
        self, client_with_mock_db: httpx.AsyncClient
    ) -> None:
        """Non-UUID values are rejected with 422 (FastAPI validation)."""
        response = await client_with_mock_db.get("/api/v1/live/trades?deployment_id=not-a-uuid")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Supervisor-liveness guard (drill 2026-04-15 P0-A)
# ---------------------------------------------------------------------------


class TestLiveStartDeprecated:
    """POST /live/start is deprecated and must return 410 Gone."""

    async def test_live_start_returns_410(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /api/v1/live/start returns 410 with deprecation notice."""
        response = await client_with_mock_db.post(
            "/api/v1/live/start",
            json={
                "strategy_id": "00000000-0000-0000-0000-000000000001",
                "config": {},
                "instruments": ["AAPL"],
                "paper_trading": True,
            },
        )

        assert response.status_code == 410
        body = response.json()
        assert body["detail"]["error"]["code"] == "ENDPOINT_DEPRECATED"
        assert "start-portfolio" in body["detail"]["error"]["message"]


class TestStartPortfolioLiveBlocked:
    """POST /api/v1/live/start-portfolio rejects ``paper_trading=false`` with
    503 LIVE_DEPLOY_BLOCKED until the snapshot-binding follow-up lands
    (Codex Contrarian's blocking objection #1 from the 2026-05-13 graduation-
    gate council). The guard MUST fire BEFORE the idempotency layer so a
    cached outcome can never replay a paper_trading=false response.
    """

    @pytest.mark.asyncio
    async def test_live_paper_trading_false_returns_503(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        response = await client_with_mock_db.post(
            "/api/v1/live/start-portfolio",
            json={
                "portfolio_revision_id": "00000000-0000-0000-0000-000000000002",
                "account_id": "U1234567",
                "paper_trading": False,
                "ib_login_key": "test-user",
            },
        )
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["detail"]["error"]["code"] == "LIVE_DEPLOY_BLOCKED"
        assert "snapshot" in body["detail"]["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_live_paper_trading_false_blocked_before_idempotency(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """Replay with same Idempotency-Key still returns 503; the guard
        fires before any reservation is recorded so there's no cached
        outcome to replay."""
        body = {
            "portfolio_revision_id": "00000000-0000-0000-0000-000000000002",
            "account_id": "U1234567",
            "paper_trading": False,
            "ib_login_key": "test-user",
        }
        headers = {"Idempotency-Key": "test-replay-key-001"}
        r1 = await client_with_mock_db.post(
            "/api/v1/live/start-portfolio", json=body, headers=headers
        )
        r2 = await client_with_mock_db.post(
            "/api/v1/live/start-portfolio", json=body, headers=headers
        )
        assert r1.status_code == 503
        assert r2.status_code == 503
        assert r1.json()["detail"]["error"]["code"] == "LIVE_DEPLOY_BLOCKED"
        assert r2.json()["detail"]["error"]["code"] == "LIVE_DEPLOY_BLOCKED"
