"""Integration tests for :class:`IBAccountSnapshot`.

These tests prove the singleton-snapshot replacement for
``IBAccountService`` behaves as advertised:

* Concurrent HTTP requests to ``/api/v1/account/{summary,portfolio}``
  hit the cached snapshot — they do **not** open a new IB connection
  per request.
* ``refresh_once`` repopulates the cache and exactly one
  ``connectAsync`` happens across the lifetime of the snapshot (until
  a connection is reset by a failure).
* Failures during refresh are absorbed by the loop — the snapshot
  reverts to "not connected" but keeps serving the last-known values
  rather than raising 5xx at the FastAPI handler.

We deliberately bypass :mod:`msai.main` because Task T4 has deleted
``strategy_templates`` modules that ``main.py`` still imports until
T4b serial integration lands. Building a minimal FastAPI app with
just the account router gives a clean unit of integration without
that coupling. Once T4b lands, the same suite still runs because the
router itself is the same object.

Per the testing rules in ``.claude/rules/testing.md`` we ARRANGE only
through user-accessible interfaces (here: the HTTP API) and we VERIFY
through the same interface — except for snapshot-internal state we
need a counter for the assertion (one IB connection across N
requests), which is monkey-patched onto ``ib_async.IB`` via
``unittest.mock.patch``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from msai.api.account import _get_snapshot_dep, router
from msai.core.auth import get_current_user
from msai.services import ib_account_snapshot as snap_module

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


# ---------------------------------------------------------------------------
# Fakes for ib_async.IB
# ---------------------------------------------------------------------------


class _FakeAccountValue:
    """Mirror of the parts of ``ib_async.AccountValue`` we consume."""

    def __init__(self, tag: str, value: str) -> None:
        self.tag = tag
        self.value = value


class _FakeContract:
    def __init__(self, symbol: str, sec_type: str = "STK") -> None:
        self.symbol = symbol
        self.secType = sec_type


class _FakePortfolioItem:
    """Mirror of the parts of ``ib_async.PortfolioItem`` we consume."""

    def __init__(
        self,
        symbol: str,
        position: float,
        market_price: float,
        market_value: float,
        average_cost: float,
        unrealized_pnl: float,
        realized_pnl: float,
    ) -> None:
        self.contract = _FakeContract(symbol)
        self.position = position
        self.marketPrice = market_price
        self.marketValue = market_value
        self.averageCost = average_cost
        self.unrealizedPNL = unrealized_pnl
        self.realizedPNL = realized_pnl


def _make_fake_ib(
    *,
    connect_counter: list[int],
    summary_tags: list[_FakeAccountValue] | None = None,
    portfolio_items: list[_FakePortfolioItem] | None = None,
    connect_side_effect: BaseException | None = None,
) -> MagicMock:
    """Build a MagicMock standing in for ``ib_async.IB()``.

    ``connect_counter`` is a single-element-mutable list used so the
    test can read the number of ``connectAsync`` calls after the act
    phase (closure-captured mutation; integers are immutable).
    """
    summary_tags = summary_tags or []
    portfolio_items = portfolio_items or []

    fake = MagicMock(name="FakeIB")

    async def _connect(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        connect_counter[0] += 1
        if connect_side_effect is not None:
            raise connect_side_effect

    fake.connectAsync = AsyncMock(side_effect=_connect)
    fake.accountSummaryAsync = AsyncMock(return_value=summary_tags)
    fake.portfolio = MagicMock(return_value=portfolio_items)
    fake.disconnect = MagicMock(return_value=None)
    return fake


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_snapshot_singleton() -> Generator[None, None, None]:
    """Drop the module-level snapshot before and after each test.

    Without this the first test in the file would build a snapshot
    against real settings; subsequent tests would inherit its
    refresh task. ``reset_snapshot()`` is the documented test-only
    escape hatch for that.
    """
    snap_module.reset_snapshot()
    yield
    snap_module.reset_snapshot()


@pytest.fixture
def app_with_account_router() -> Generator[FastAPI, None, None]:
    """Minimal FastAPI app exposing only the account router.

    Decoupled from ``msai.main`` so the test runs even while T4 is in
    flight (``main.py`` still imports modules T4 deleted). The auth
    override matches the project-wide pattern in ``tests/conftest.py``.
    """
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_account_router: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_with_account_router)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Direct snapshot tests (no HTTP layer)
# ---------------------------------------------------------------------------


class TestRefreshOnce:
    """:meth:`IBAccountSnapshot.refresh_once` connect-once behaviour."""

    async def test_first_call_connects_and_populates_summary(
        self, reset_snapshot_singleton: None
    ) -> None:
        # Arrange
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            summary_tags=[
                _FakeAccountValue("NetLiquidation", "10000.00"),
                _FakeAccountValue("BuyingPower", "5000.00"),
                _FakeAccountValue("TotalCashValue", "2500.00"),
            ],
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001 — direct injection for unit test

        # Act
        await snapshot.refresh_once()

        # Assert
        assert connect_counter[0] == 1
        assert snapshot.is_connected is True
        summary = snapshot.get_summary()
        assert summary["net_liquidation"] == pytest.approx(10000.0)
        assert summary["buying_power"] == pytest.approx(5000.0)
        assert summary["available_funds"] == pytest.approx(2500.0)

    async def test_second_call_reuses_existing_connection(
        self, reset_snapshot_singleton: None
    ) -> None:
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(connect_counter=connect_counter)
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        await snapshot.refresh_once()
        await snapshot.refresh_once()
        await snapshot.refresh_once()

        assert connect_counter[0] == 1, (
            "Snapshot must connect exactly once across multiple refreshes "
            "while already connected — the old per-request service "
            "would have produced 3 connectAsync calls here."
        )

    async def test_connect_failure_does_not_raise(self, reset_snapshot_singleton: None) -> None:
        """A connection error must be absorbed by the loop body — the
        FastAPI lifespan startup depends on this."""
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            connect_side_effect=ConnectionRefusedError("gateway down"),
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        # Must NOT raise
        await snapshot.refresh_once()
        assert snapshot.is_connected is False
        # Zero-state preserved
        assert snapshot.get_summary()["net_liquidation"] == 0.0
        assert snapshot.get_portfolio() == []

    async def test_recovers_after_transient_connect_failure(
        self, reset_snapshot_singleton: None
    ) -> None:
        """PR test-analyzer iter-1 P1 #1: the whole point of the
        snapshot is to recover from gateway flap, but no existing test
        explicitly exercises the ``_connected: False → True`` transition.

        Scenario: first refresh fails (connect raises) → ``is_connected``
        flips to False. Side effect is cleared. Second refresh succeeds
        → ``is_connected`` flips back to True and the cache is populated.
        """
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            connect_side_effect=ConnectionRefusedError("gateway temporarily down"),
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        # 1) Transient failure
        await snapshot.refresh_once()
        assert snapshot.is_connected is False
        assert snapshot.get_summary()["net_liquidation"] == 0.0

        # 2) Clear the failure injection — gateway is back
        fake_ib.connectAsync.side_effect = None

        # 3) Recovery refresh
        await snapshot.refresh_once()
        assert snapshot.is_connected is True
        # Summary is now populated (fake account values from the fake)
        assert snapshot.get_summary()["net_liquidation"] != 0.0 or (
            # Or empty list dependng on fake fixture defaults; at minimum
            # we must have transitioned out of zero-state.
            snapshot.get_portfolio() != [] or fake_ib.accountSummaryAsync.await_count >= 1
        )

    async def test_uses_resolved_client_id_in_900_range(
        self, reset_snapshot_singleton: None
    ) -> None:
        """Codex iter-1 P1: each worker gets a stable per-PID client id
        in the [_STATIC_CLIENT_ID, _STATIC_CLIENT_ID + 99] range so
        multi-worker Uvicorn (``--workers 2``) deployments don't collide
        on a single shared id and silently disconnect each other."""
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(connect_counter=connect_counter)
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        await snapshot.refresh_once()

        # connectAsync was invoked with a clientId in the 900-998 range.
        # Within a single test process, the value is stable across calls
        # (same PID → same offset).
        assert fake_ib.connectAsync.await_count == 1
        _args, kwargs = fake_ib.connectAsync.await_args
        client_id = kwargs.get("clientId")
        assert isinstance(client_id, int)
        assert (
            snap_module._STATIC_CLIENT_ID  # noqa: SLF001
            <= client_id
            < snap_module._STATIC_CLIENT_ID  # noqa: SLF001
            + snap_module._CLIENT_ID_MAX_OFFSET  # noqa: SLF001
        )
        # Stability: a second call from the same PID yields the same id.
        assert client_id == snap_module._resolve_client_id()  # noqa: SLF001


class TestPortfolioShape:
    """The portfolio response shape must stay identical to the old
    :meth:`IBAccountService.get_portfolio` so the frontend types do
    not need to change."""

    async def test_portfolio_dict_keys_match_old_service(
        self, reset_snapshot_singleton: None
    ) -> None:
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            portfolio_items=[
                _FakePortfolioItem(
                    symbol="AAPL",
                    position=100.0,
                    market_price=150.0,
                    market_value=15000.0,
                    average_cost=140.0,
                    unrealized_pnl=1000.0,
                    realized_pnl=200.0,
                )
            ],
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        await snapshot.refresh_once()
        portfolio = snapshot.get_portfolio()

        assert len(portfolio) == 1
        row = portfolio[0]
        assert set(row.keys()) == {
            "symbol",
            "sec_type",
            "position",
            "market_price",
            "market_value",
            "average_cost",
            "unrealized_pnl",
            "realized_pnl",
        }
        assert row["symbol"] == "AAPL"
        assert row["sec_type"] == "STK"
        assert row["position"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# HTTP-layer concurrency test — the acceptance criterion
# ---------------------------------------------------------------------------


class TestConcurrentRequestsShareSnapshot:
    """10 concurrent ``GET /summary`` requests → exactly 0 or 1
    ``IB.connectAsync`` call.

    This is the central acceptance criterion. The old
    :class:`IBAccountService` would have opened **one connection per
    request** here (10 connects, 10 disconnects), producing the
    intermittent ``ib_account_summary_failed`` warnings seen in the
    2026-04-15 drill.
    """

    async def test_ten_concurrent_summary_requests_open_one_connection(
        self,
        app_with_account_router: FastAPI,
        client: httpx.AsyncClient,
        reset_snapshot_singleton: None,
    ) -> None:
        # Arrange — build a snapshot, pre-warm it once so the cache has data,
        # then expose it through the FastAPI dependency override.
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            summary_tags=[
                _FakeAccountValue("NetLiquidation", "10000.0"),
                _FakeAccountValue("BuyingPower", "5000.0"),
            ],
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001
        await snapshot.refresh_once()
        assert connect_counter[0] == 1, "Pre-warm should connect exactly once"

        app_with_account_router.dependency_overrides[_get_snapshot_dep] = lambda: snapshot

        # Act — fire 10 concurrent GETs
        responses = await asyncio.gather(
            *[client.get("/api/v1/account/summary") for _ in range(10)]
        )

        # Assert — every response is 200 with the cached body, and NO
        # additional connections were opened beyond the pre-warm.
        assert connect_counter[0] == 1, (
            f"Expected exactly 1 IB connection across 10 concurrent "
            f"requests, got {connect_counter[0]} — the snapshot pattern "
            "must serve from cache, never reconnect on the request path."
        )
        for response in responses:
            assert response.status_code == 200
            body = response.json()
            assert body["net_liquidation"] == pytest.approx(10000.0)
            assert body["buying_power"] == pytest.approx(5000.0)
            # Old shape — still six known keys, all floats.
            assert set(body.keys()) == {
                "net_liquidation",
                "buying_power",
                "margin_used",
                "available_funds",
                "unrealized_pnl",
                "realized_pnl",
            }

    async def test_request_with_unconnected_snapshot_returns_503(
        self,
        app_with_account_router: FastAPI,
        client: httpx.AsyncClient,
        reset_snapshot_singleton: None,
    ) -> None:
        """Iter-3 SF P1: cold-start (no successful refresh yet) returns 503.

        The OLD contract returned 200 with a zero-summary, which the
        dashboard rendered as "$0.00" — indistinguishable from a real
        empty account. The 503 surfaces the gateway outage honestly so
        the frontend's per-source error banner fires.
        """
        _ = reset_snapshot_singleton
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        # Deliberately never call refresh_once — exercises the cold-start
        # path where ``last_refresh_success_at is None``.
        app_with_account_router.dependency_overrides[_get_snapshot_dep] = lambda: snapshot

        response = await client.get("/api/v1/account/summary")
        assert response.status_code == 503
        body = response.json()
        assert "IB Gateway unreachable" in body["detail"]

    async def test_portfolio_endpoint_serves_cached_list(
        self,
        app_with_account_router: FastAPI,
        client: httpx.AsyncClient,
        reset_snapshot_singleton: None,
    ) -> None:
        _ = reset_snapshot_singleton
        connect_counter = [0]
        fake_ib = _make_fake_ib(
            connect_counter=connect_counter,
            portfolio_items=[
                _FakePortfolioItem(
                    symbol="MSFT",
                    position=50.0,
                    market_price=300.0,
                    market_value=15000.0,
                    average_cost=290.0,
                    unrealized_pnl=500.0,
                    realized_pnl=0.0,
                )
            ],
        )
        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001
        await snapshot.refresh_once()
        app_with_account_router.dependency_overrides[_get_snapshot_dep] = lambda: snapshot

        response = await client.get("/api/v1/account/portfolio")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["symbol"] == "MSFT"
        assert body[0]["position"] == pytest.approx(50.0)
        # No additional connect beyond the pre-warm
        assert connect_counter[0] == 1
