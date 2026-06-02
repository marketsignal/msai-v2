"""F4 fix (Codex iter 2 P1 / silent-failure-hunter F1) + iter 14 P2.

The ``/api/v1/live/drain/{account_id}`` endpoint must:

1. Not swallow STOP-publish failures (F4 — 207/503 escalation).
2. Not report drain success until the supervisor confirmed each
   deployment is flat (Codex iter 14 P2 — 207 ``DRAIN_INCOMPLETE_FLATNESS``).

The drain path was refactored to mirror ``/kill-all``: it uses
``coalesce_or_publish_stop_with_flatness`` + ``poll_stop_report`` so the
response body carries the per-deployment flatness summary. These tests
stub the new functions at the live module's namespace so the endpoint
can be exercised without booting Redis or the supervisor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import redis
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api.live_deps import get_command_bus
from msai.core.database import get_db
from msai.main import app
from msai.models.live_deployment import LiveDeployment
from msai.services.live_command_bus import LiveCommandBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
def mock_db_with_active_deployments() -> AsyncMock:
    """Return a mock DB whose ``execute`` returns two active LiveDeployment
    rows matching the drained account_id."""
    session = AsyncMock(spec=AsyncSession)
    dep_a = LiveDeployment(id=uuid4(), account_id="DUP733214", status="running")
    dep_b = LiveDeployment(id=uuid4(), account_id="DUP733214", status="ready")

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [dep_a, dep_b]
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalar_one.return_value = None

    session.execute = AsyncMock(return_value=mock_result)
    session._test_deployments = [dep_a, dep_b]  # type: ignore[attr-defined]
    return session


@pytest.fixture
def mock_bus_publishes_fail(mock_db_with_active_deployments: AsyncMock) -> MagicMock:
    """Stub LiveCommandBus where the FIRST publish raises a RedisError
    and the SECOND succeeds. Used to test the 207 partial-failure path."""
    bus = MagicMock(spec=LiveCommandBus)
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.delete = AsyncMock(return_value=1)
    fake_redis.exists = AsyncMock(return_value=0)
    bus._redis = fake_redis  # noqa: SLF001
    return bus


@pytest.fixture
def client(
    mock_db_with_active_deployments: AsyncMock,
    mock_bus_publishes_fail: MagicMock,
) -> httpx.AsyncClient:
    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db_with_active_deployments

    async def _override_get_bus() -> LiveCommandBus:
        return mock_bus_publishes_fail

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://testserver")  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)


@pytest.fixture
def _patch_flatness(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Patch the flatness + terminal-poll helpers at the ``live``
    module's namespace so the drain endpoint runs without Redis or the
    supervisor's fleet_router. Returns the mock handles so tests
    can configure per-call side effects.

    By default ``_poll_for_terminal`` returns a synthetic row whose
    presence signals ``terminal_confirmed=True``. Tests that want the
    timeout path override the return value to ``None``.
    """
    coalesce = AsyncMock()
    poll = AsyncMock()
    poll_terminal = AsyncMock(return_value=MagicMock())  # truthy row = terminal
    monkeypatch.setattr(
        "msai.api.live.coalesce_or_publish_stop_with_flatness",
        coalesce,
    )
    monkeypatch.setattr("msai.api.live.poll_stop_report", poll)
    monkeypatch.setattr("msai.api.live._poll_for_terminal", poll_terminal)
    return {"coalesce": coalesce, "poll": poll, "poll_terminal": poll_terminal}


@pytest.mark.asyncio
async def test_drain_returns_207_when_one_publish_fails(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """F4: partial publish failure → 207 Multi-Status with BOTH the
    ``stopped`` and ``failed`` arrays populated."""
    _patch_flatness["coalesce"].side_effect = [
        redis.RedisError("redis blip"),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = {
        "broker_flat": True,
        "remaining_positions": [],
    }

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 207
    body: dict[str, Any] = response.json()
    assert body["account_id"] == "DUP733214"
    assert len(body["stopped"]) == 1
    assert len(body["failed"]) == 1
    assert "deployment_id" in body["failed"][0]
    assert "error" in body["failed"][0]
    assert body["error"]["code"] == "DRAIN_PARTIAL_FAILURE"


@pytest.mark.asyncio
async def test_drain_returns_503_when_all_publishes_fail(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """F4: total publish failure → 503 with empty ``stopped`` and both
    deployments listed in ``failed``."""
    _patch_flatness["coalesce"].side_effect = [
        redis.RedisError("blip A"),
        redis.RedisError("blip B"),
    ]

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 503
    body: dict[str, Any] = response.json()
    assert body["stopped"] == []
    assert len(body["failed"]) == 2
    assert body["error"]["code"] == "DRAIN_PARTIAL_FAILURE"


@pytest.mark.asyncio
async def test_drain_returns_200_on_clean_flat_path(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """All publishes succeed AND both flatness reports show
    ``broker_flat=True`` → 200 with empty ``failed`` and a populated
    ``flatness_reports`` array."""
    _patch_flatness["coalesce"].side_effect = [
        ("nonce-1", True),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = {
        "broker_flat": True,
        "remaining_positions": [],
    }

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["account_id"] == "DUP733214"
    assert len(body["stopped"]) == 2
    assert body["failed"] == []
    assert body["any_non_flat"] is False
    assert len(body["flatness_reports"]) == 2


@pytest.mark.asyncio
async def test_drain_returns_207_when_flatness_unknown(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """Codex iter 14 P2: STOP commands published but the supervisor
    didn't write flatness reports within the deadline → 207
    ``DRAIN_INCOMPLETE_FLATNESS``. The drain is NOT done; the operator
    must verify positions via the IB portal."""
    _patch_flatness["coalesce"].side_effect = [
        ("nonce-1", True),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = None  # no report within deadline

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 207
    body: dict[str, Any] = response.json()
    assert body["any_non_flat"] is True
    assert body["error"]["code"] == "DRAIN_INCOMPLETE_FLATNESS"


@pytest.mark.asyncio
async def test_drain_rejects_whitespace_account_id(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """Codex iter 17 P2: a percent-encoded path like
    ``%20DUP733214%20`` used to set ``account_halt_key(' DUP733214 ')``
    while the supervisor read ``account_halt_key('DUP733214')`` (the
    ``PortfolioStartRequest`` validator strips at the start path). Drain
    latch bypassed. Endpoint now mirrors the schema-layer normalization
    — strip + reject any remaining whitespace at the path layer."""
    response = await client.post("/api/v1/live/drain/%20DUP%20733214%20")
    assert response.status_code == 422
    body: dict[str, Any] = response.json()
    assert "whitespace" in str(body.get("detail", "")).lower()


@pytest.mark.asyncio
async def test_drain_strips_padding_account_id(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
    mock_db_with_active_deployments: AsyncMock,
) -> None:
    """Codex iter 17 P2: leading/trailing whitespace ONLY (no internal
    whitespace) is stripped — the resulting key matches what
    ``PortfolioStartRequest`` writes for the same account.
    Account ``%20DUP733214%20`` (which decodes to ``' DUP733214 '``)
    normalizes to ``DUP733214``."""
    _patch_flatness["coalesce"].side_effect = [("nonce-1", True), ("nonce-2", True)]
    _patch_flatness["poll"].return_value = {
        "broker_flat": True,
        "remaining_positions": [],
    }
    response = await client.post("/api/v1/live/drain/%20DUP733214%20")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["account_id"] == "DUP733214"


@pytest.mark.asyncio
async def test_drain_syncs_deployment_status_after_terminal(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
    mock_db_with_active_deployments: AsyncMock,
) -> None:
    """Codex iter 20 P2: ``_poll_for_terminal`` only confirms the
    ``live_node_processes`` row reached terminal status — the parent
    ``LiveDeployment.status`` can stay ``'running'`` if the child writes
    a flat report and exits before its ``_mark_terminal`` parent sync.
    ``/stop`` mirrors this guard by setting ``deployment.status='stopped'``;
    drain must too, otherwise ``/api/v1/live/status`` keeps showing
    drained accounts as active.

    Test contract: assert the endpoint calls ``db.get(LiveDeployment, ...)``
    for each terminal-confirmed deployment (proves the sync code path
    runs) and that the seeded rows had their ``status`` field assigned.
    """
    _patch_flatness["coalesce"].side_effect = [
        ("nonce-1", True),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = {
        "broker_flat": True,
        "remaining_positions": [],
    }
    # Terminal poll returns a truthy row → terminal_confirmed=True.

    # Stub db.get so it returns the seeded deployments (instead of a
    # bare AsyncMock auto-attr) — then the endpoint's status-sync write
    # mutates the real test rows.
    deployments = mock_db_with_active_deployments._test_deployments  # noqa: SLF001
    by_id = {dep.id: dep for dep in deployments}

    async def _get_dep(_model: object, dep_id: object) -> object:
        return by_id.get(dep_id)

    mock_db_with_active_deployments.get = AsyncMock(side_effect=_get_dep)

    response = await client.post("/api/v1/live/drain/DUP733214")
    assert response.status_code == 200

    for dep in deployments:
        assert dep.status == "stopped"
        assert dep.last_stopped_at is not None


@pytest.mark.asyncio
async def test_drain_returns_207_when_terminal_state_not_reached(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """Codex iter 18 P2: a flat stop_report alone is insufficient — the
    child could write the report and then hang in dispose(), leaving
    the ``live_node_processes`` row in ``running`` status while the API
    reports drain success. ``/stop`` polls the row to terminal; drain
    now does the same. When ``_poll_for_terminal`` returns ``None``
    (timeout), the drain must surface 207 ``DRAIN_INCOMPLETE_FLATNESS``
    even with a flat report.
    """
    _patch_flatness["coalesce"].side_effect = [
        ("nonce-1", True),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = {
        "broker_flat": True,
        "remaining_positions": [],
    }
    # Terminal-state poll times out → row never reached stopped/failed.
    _patch_flatness["poll_terminal"].return_value = None

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 207
    body: dict[str, Any] = response.json()
    assert body["any_non_flat"] is True
    assert body["error"]["code"] == "DRAIN_INCOMPLETE_FLATNESS"
    # Every flatness summary entry must carry the terminal-confirmed
    # field so operators can disambiguate "supervisor stopped" vs
    # "supervisor reported flat but didn't terminate" in the response.
    for entry in body["flatness_reports"]:
        assert entry["terminal_confirmed"] is False


@pytest.mark.asyncio
async def test_drain_returns_207_when_positions_remain(
    client: httpx.AsyncClient,
    _patch_flatness: dict[str, AsyncMock],
) -> None:
    """Codex iter 14 P2: supervisor reports remaining positions →
    207 ``DRAIN_INCOMPLETE_FLATNESS``. Operator gets the truth instead
    of a 200 that masks unflattened positions."""
    _patch_flatness["coalesce"].side_effect = [
        ("nonce-1", True),
        ("nonce-2", True),
    ]
    _patch_flatness["poll"].return_value = {
        "broker_flat": False,
        "remaining_positions": [{"symbol": "AAPL", "qty": 100}],
    }

    response = await client.post("/api/v1/live/drain/DUP733214")

    assert response.status_code == 207
    body: dict[str, Any] = response.json()
    assert body["any_non_flat"] is True
    assert body["error"]["code"] == "DRAIN_INCOMPLETE_FLATNESS"
    assert len(body["flatness_reports"][0]["remaining_positions"]) >= 1
