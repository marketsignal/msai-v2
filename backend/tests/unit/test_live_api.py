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
# Helpers
# ---------------------------------------------------------------------------


def _kill_all_execute_side_effect(rows: list[tuple[object, str]]):
    """Build a ``db.execute`` side_effect for the PR 2 T4 /kill-all path.

    The active-process query is joined to ``live_deployments`` and returns
    ``(LiveNodeProcess, account_id)`` 2-tuples via ``result.all()``. Other
    ``execute`` calls (the user lookup, the per-row member-strategy lookup)
    are matched by statement text and served defaults so they don't consume
    the tuple-result slot. Matching on the rendered SQL is order-independent.
    """

    async def _execute(statement: object = "", *_a: object, **_k: object) -> MagicMock:
        sql = str(statement).lower()
        result = MagicMock()
        result.scalar_one.return_value = 0
        result.scalar_one_or_none.return_value = None
        if "live_node_processes" in sql and "join" in sql:
            result.all.return_value = rows
        else:
            result.all.return_value = []
        return result

    return _execute


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
    # PR 1b T6: /kill-all uses eval(HALT_WRITE_LUA, ...) (returns 1) and
    # /resume uses eval(RESUME_CLEAR_LUA, ...) (returns "OK" on a clean
    # clear). With the unit mock_db returning ZERO active deployments,
    # /resume probes no manifests and the clear is vacuously "OK".
    fake_redis.eval = AsyncMock(return_value="OK")

    async def _empty_scan_iter(*_a: object, **_k: object):
        # /resume scans `stop_unknown:*`; yield nothing for the unit path.
        return
        yield  # pragma: no cover — makes this an async generator

    fake_redis.scan_iter = _empty_scan_iter
    # Flatness service (Bug #2) uses get/rpush/expire on bus._redis.
    # Default `get` returns a stub report so the API doesn't wait for
    # the 15-30s deadline on every existing test. Tests that care
    # about the flatness wire override fake_redis.get explicitly.
    import json as _json

    fake_redis.get = AsyncMock(
        return_value=_json.dumps(
            {
                "stop_nonce": "stub",
                "deployment_id": "stub",
                "broker_flat": True,
                "remaining_positions": [],
                "reason": "ok",
                "reported_at": "2026-05-13T00:00:00+00:00",
            }
        )
    )
    fake_redis.rpush = AsyncMock(return_value=1)
    fake_redis.expire = AsyncMock(return_value=True)

    # F9 fix support: /kill-all now batches halt-set writes via a
    # transactional pipeline (MULTI/EXEC). Provide an async-context-
    # manager-friendly pipeline that records each ``set(...)`` call
    # so tests can still assert "msai:risk:halt was written with 24h
    # TTL" without caring whether the call went through bus._redis.set
    # directly or through the pipeline.
    pipeline_set_calls: list[tuple[str, str, dict[str, object]]] = []

    class _FakePipeline:
        def set(self, key: str, value: str, **kw: object) -> _FakePipeline:
            pipeline_set_calls.append((key, value, dict(kw)))
            return self

        async def execute(self) -> list[object]:
            return [True] * len(pipeline_set_calls)

        async def __aenter__(self) -> _FakePipeline:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    fake_redis.pipeline = MagicMock(return_value=_FakePipeline())
    fake_redis._pipeline_set_calls = pipeline_set_calls
    bus._redis = fake_redis  # noqa: SLF001
    bus.publish_stop_and_report_flatness = AsyncMock(return_value="1-1")
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

    # Task 5: /start-portfolio reads app.state.gateway_router (set during the
    # app lifespan in production). The unit fixture doesn't run the lifespan, so
    # set an empty router here so the handler's eager read doesn't AttributeError.
    from msai.services.live.gateway_router import GatewayRouter

    app.state.gateway_router = GatewayRouter(None)

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
        kill-all so subsequent ``/start`` calls return 503.

        PR 1b T6: kill-all now writes the halt keyset via the atomic
        ``HALT_WRITE_LUA`` script (one ``eval`` round-trip) instead of a
        MULTI/EXEC pipeline. The latch key + 24h TTL are passed as
        eval KEYS/ARGV. Inspect the recorded eval call.
        """
        await client_with_mock_db.post("/api/v1/live/kill-all")

        eval_call = mock_command_bus._redis.eval.call_args  # noqa: SLF001
        assert eval_call is not None
        # eval(script, numkeys, *keys, *argv) — the latch key is KEYS[1].
        args = eval_call.args
        numkeys = args[1]
        keys = list(args[2 : 2 + numkeys])
        argv = list(args[2 + numkeys :])
        assert "msai:risk:halt" in keys
        # The 24h TTL (last ARGV) is applied to the halt keyset.
        assert "86400" in argv

    async def test_kill_all_uses_atomic_halt_write_lua(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """PR 1b T6: the halt-set writes (latch, set_by, set_at,
        cause, cause-history) MUST go through the shared atomic
        ``HALT_WRITE_LUA`` script so the keyset is all-or-nothing AND the
        cause is written ONLY-IF-ABSENT — preserving a pre-existing
        data-stale auto-halt's attribution instead of erasing it. The
        previous MULTI/EXEC pipeline blindly SET the cause key, silently
        clobbering a data-stale cause on a manual kill-all.
        """
        from msai.core.halt_keys import HALT_WRITE_LUA

        await client_with_mock_db.post("/api/v1/live/kill-all")

        eval_call = mock_command_bus._redis.eval.call_args  # noqa: SLF001
        assert eval_call is not None
        # The shared atomic script is used (NOT a transactional pipeline).
        assert eval_call.args[0] == HALT_WRITE_LUA
        assert mock_command_bus._redis.pipeline.called is False  # noqa: SLF001

        numkeys = eval_call.args[1]
        keys = set(eval_call.args[2 : 2 + numkeys])
        # All five halt-related keys are written in the same atomic call.
        assert "msai:risk:halt" in keys
        assert "msai:risk:halt:set_by" in keys
        assert "msai:risk:halt:set_at" in keys
        assert "msai:risk:halt:cause" in keys
        assert "msai:risk:halt:cause:history" in keys

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

        # PR 2 T4: /kill-all now selects (LiveNodeProcess, account_id) via a
        # join to live_deployments, so the result rows are 2-tuples consumed
        # via ``result.all()`` (not ``.scalars().all()``). The per-row member
        # lookup ALSO uses ``.all()``, so route the main query result via a
        # side_effect (first call) and return an empty member result after.
        rows = [
            (LiveNodeProcess(deployment_id=uuid4(), status="running"), "DUP-A"),
            (LiveNodeProcess(deployment_id=uuid4(), status="ready"), "DUP-B"),
            (LiveNodeProcess(deployment_id=uuid4(), status="building"), "DUP-C"),
        ]
        mock_db.execute.side_effect = _kill_all_execute_side_effect(rows)

        response = await client_with_mock_db.post("/api/v1/live/kill-all")

        assert response.status_code == 200
        body = response.json()
        assert body["stopped"] == 3
        # Bug #2 (live-deploy-safety-trio): /kill-all publishes
        # STOP_AND_REPORT_FLATNESS, not plain STOP.
        assert mock_command_bus.publish_stop_and_report_flatness.await_count == 3
        expected_accounts = {"DUP-A", "DUP-B", "DUP-C"}
        seen_accounts: set[str] = set()
        for call in mock_command_bus.publish_stop_and_report_flatness.await_args_list:
            assert call.kwargs.get("reason") == "kill_switch"
            # PR 2 T4: each STOP routes onto the process's per-account stream.
            seen_accounts.add(call.kwargs.get("account_id"))
        assert seen_accounts == expected_accounts

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

        # PR 2 T4: (LiveNodeProcess, account_id) 2-tuples via result.all().
        rows = [
            (LiveNodeProcess(deployment_id=uuid4(), status="running"), "DUP-A"),
            (LiveNodeProcess(deployment_id=uuid4(), status="running"), "DUP-B"),
        ]
        mock_db.execute.side_effect = _kill_all_execute_side_effect(rows)

        # First call raises, second succeeds. /kill-all now uses
        # publish_stop_and_report_flatness (Bug #2).
        mock_command_bus.publish_stop_and_report_flatness.side_effect = [
            RuntimeError("redis blip"),
            "1-0",
        ]

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

    async def test_resume_clears_halt_flag_via_atomic_lua(
        self,
        client_with_mock_db: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """PR 1b T6: with zero active deployments (mock_db default), resume
        is a vacuous pass — it clears the halt keyset via the atomic
        ``RESUME_CLEAR_LUA`` script. The latch + metadata + cause +
        history + legacy transition-compat keys are passed as the script's
        DELETE keys (the tail of KEYS after the manifest/verdict/reconciled
        segments, all of which are empty here)."""
        from msai.core.halt_keys import RESUME_CLEAR_LUA

        await client_with_mock_db.post("/api/v1/live/resume")

        eval_call = mock_command_bus._redis.eval.call_args  # noqa: SLF001
        assert eval_call is not None
        assert eval_call.args[0] == RESUME_CLEAR_LUA
        numkeys = eval_call.args[1]
        keys = set(eval_call.args[2 : 2 + numkeys])
        # ARGV segment counts are all zero (no active deployments).
        argv = list(eval_call.args[2 + numkeys :])
        assert argv == ["0", "0", "0"]
        # The halt keyset is the script's delete-list.
        assert "msai:risk:halt" in keys
        assert "msai:risk:halt:set_by" in keys
        assert "msai:risk:halt:set_at" in keys
        assert "msai:risk:halt:cause" in keys


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


class TestStartPortfolioPreReserveBinding:
    """POST /api/v1/live/start-portfolio runs snapshot binding BEFORE
    the idempotency reservation (Bug #3, replaces PR #63's temporary
    503 LIVE_DEPLOY_BLOCKED guard). A non-existent revision now
    surfaces as 404 from the pre-reserve loader; an unmatched
    candidate surfaces as 422 BINDING_NOT_GRADUATED.
    """

    @pytest.mark.asyncio
    async def test_new_freeform_deploy_with_unresolvable_account_422_before_binding(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """Task 5 reordered account resolution to the TOP of the handler:
        a NEW free-form (legacy-strings) deploy whose ``account_id`` does not
        resolve to an ACTIVE broker account now fails closed with 422
        BROKER_ACCOUNT_NOT_RESOLVABLE BEFORE the revision lookup / idempotency
        reservation (council mandate — new free-form deploys must resolve or
        fail closed). The mock DB returns ``scalar_one_or_none=None`` for every
        query, so the warm-restart identity lookup misses (→ NEW deploy) and the
        registry lookup also misses (→ unresolvable)."""
        response = await client_with_mock_db.post(
            "/api/v1/live/start-portfolio",
            json={
                "portfolio_revision_id": "00000000-0000-0000-0000-000000000002",
                "account_id": "U1234567",
                "paper_trading": False,
                "ib_login_key": "test-user",
            },
        )
        assert response.status_code == 422, response.text
        assert "BROKER_ACCOUNT_NOT_RESOLVABLE" in response.text
