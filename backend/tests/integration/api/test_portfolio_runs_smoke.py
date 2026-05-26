"""smoke=True flag plumbing through portfolio-run API.

Verifies the additive ``smoke: bool`` field on ``PortfolioRunCreate`` /
``PortfolioRunResponse``:

- ``smoke=True`` in the POST body persists and surfaces on the response.
- Omitting ``smoke`` defaults to ``False`` (additive, non-breaking).

The endpoint at ``POST /api/v1/portfolios/{portfolio_id}/runs`` enqueues
the run via arq → Redis. Redis is not guaranteed in the integration test
runtime, so the enqueue path is monkeypatched out so the test exercises
the schema + lifecycle plumbing under the standard 201 contract.

PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


@pytest.fixture
def _mock_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the arq enqueue path so the API route reaches the 201 return.

    The portfolio-runs route calls ``get_redis_pool()`` then
    ``enqueue_portfolio_run(pool, ...)``. Both live in ``msai.core.queue``
    and are imported into the route via
    ``from msai.core.queue import enqueue_portfolio_run, get_redis_pool``,
    so the patch targets the names in the API module's namespace.
    """

    class _StubPool:
        async def close(self) -> None:  # arq pools are sometimes closed
            return None

    async def _stub_get_pool() -> _StubPool:
        return _StubPool()

    async def _stub_enqueue(_pool: object, _run_id: str, _portfolio_id: str) -> str:
        return "stub-job-id"

    monkeypatch.setattr("msai.api.portfolio.get_redis_pool", _stub_get_pool)
    monkeypatch.setattr("msai.api.portfolio.enqueue_portfolio_run", _stub_enqueue)


@pytest.mark.asyncio
async def test_create_portfolio_run_with_smoke_true_persists_smoke_column(
    api_client_authed,
    sample_portfolio_id,
    _mock_enqueue: None,
) -> None:
    # Arrange / Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{sample_portfolio_id}/runs",
        json={
            "start_date": "2024-12-01",
            "end_date": "2024-12-31",
            "smoke": True,
        },
    )

    # Assert
    assert response.status_code == 201, response.text
    assert response.json()["smoke"] is True


@pytest.mark.asyncio
async def test_create_portfolio_run_smoke_defaults_false(
    api_client_authed,
    sample_portfolio_id,
    _mock_enqueue: None,
) -> None:
    # Arrange / Act
    response = await api_client_authed.post(
        f"/api/v1/portfolios/{sample_portfolio_id}/runs",
        json={
            "start_date": "2024-12-01",
            "end_date": "2024-12-31",
        },
    )

    # Assert
    assert response.status_code == 201, response.text
    assert response.json()["smoke"] is False


# ---------------------------------------------------------------------------
# T7: POST /api/v1/portfolios/smoke/runs?config=fast shortcut endpoint
#
# The route is registered BEFORE ``/{portfolio_id}/runs`` so FastAPI does not
# try to bind ``'smoke'`` as a UUID path parameter and emit a 422. The route
# delegates to ``services.smoke.runner.run_smoke()`` (no HTTP-to-self), which:
#   1) pre-ingests AAPL+SPY via ``data_ingestion.ingest_symbols`` -- mocked
#      out here so the test does not hit Databento,
#   2) bootstraps the canonical ``__msai_smoke__`` Portfolio (requires the
#      4 pre-seeded smoke Strategy rows; the Alembic seed migration is not
#      applied in the test DB which uses ``Base.metadata.create_all``, so
#      the fixture below seeds them by hand via the standard ORM),
#   3) creates a ``PortfolioRun`` with ``smoke=True`` and enqueues it -- the
#      enqueue path is the same one the existing ``_mock_enqueue`` fixture
#      monkeypatches in this file.
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ingest_symbols`` to a no-op so the smoke route never hits Databento.

    The smoke runner imports ``ingest_symbols`` at module load time, so the
    patch must target the name in ``services.smoke.runner``'s namespace.
    """
    from msai.services.data_ingestion import IngestResult

    async def _stub_ingest_symbols(  # noqa: ANN401
        _asset_class: str,
        symbols: list[str],
        _start: str,
        _end: str,
        **_kwargs: object,
    ) -> IngestResult:
        return IngestResult(
            bars_written=0,
            symbols_covered=list(symbols),
            empty_symbols=[],
        )

    monkeypatch.setattr("msai.services.smoke.runner.ingest_symbols", _stub_ingest_symbols)


@pytest.fixture
def _mock_smoke_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Redis pool + ingest-lock helpers used by the smoke runner.

    ``runner._ensure_ingested`` calls ``get_redis_pool`` + the mutex helpers
    on its own (not via the API router's imports), so the route-side
    ``_mock_enqueue`` fixture doesn't cover them.
    """

    class _StubPool:
        async def close(self) -> None:
            return None

    async def _stub_get_pool() -> _StubPool:
        return _StubPool()

    async def _stub_acquire(  # noqa: ANN401
        _pool: object,
        *,
        symbol: str,
        window_start: object,
        ttl_seconds: int,
        wait_timeout_seconds: int,
    ) -> str:
        return f"stub-token-{symbol}"

    async def _stub_release(  # noqa: ANN401
        _pool: object,
        *,
        symbol: str,
        window_start: object,
        token: str,
    ) -> bool:
        return True

    async def _stub_enqueue(_pool: object, _run_id: str, _portfolio_id: str) -> str:
        return "stub-job-id"

    monkeypatch.setattr("msai.services.smoke.runner.get_redis_pool", _stub_get_pool)
    monkeypatch.setattr("msai.services.smoke.runner.acquire_ingest_lock", _stub_acquire)
    monkeypatch.setattr("msai.services.smoke.runner.release_ingest_lock", _stub_release)
    # The runner imports enqueue_portfolio_run directly from msai.core.queue
    # (NOT via msai.api.portfolio's namespace), so _mock_enqueue's patch
    # doesn't cover this path. Patch the runner's local binding too.
    monkeypatch.setattr("msai.services.smoke.runner.enqueue_portfolio_run", _stub_enqueue)


@pytest_asyncio.fixture
async def _seed_smoke_strategies(
    portfolio_session_factory,
) -> None:
    """Seed the 4 canonical smoke Strategy rows the runner expects to find.

    The Alembic seed migration (Task 1) populates these in production. The
    test DB is built via ``Base.metadata.create_all`` (no Alembic), so the
    rows must be inserted here for ``_get_or_create_canonical_portfolio``
    to succeed.
    """
    from msai.models.strategy import Strategy

    rows = [
        {
            "name": "__smoke__/smoke_market_order/AAPL",
            "file_path": "strategies/example/smoke_market_order.py",
            "strategy_class": "SmokeMarketOrderStrategy",
            "config_class": "SmokeMarketOrderConfig",
            "default_config": {
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
        },
        {
            "name": "__smoke__/smoke_market_order/SPY",
            "file_path": "strategies/example/smoke_market_order.py",
            "strategy_class": "SmokeMarketOrderStrategy",
            "config_class": "SmokeMarketOrderConfig",
            "default_config": {
                "instrument_id": "SPY.NASDAQ",
                "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
        },
        {
            "name": "__smoke__/ema_cross/AAPL",
            "file_path": "strategies/example/ema_cross.py",
            "strategy_class": "EMACrossStrategy",
            "config_class": "EMACrossConfig",
            "default_config": {
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 20,
                "trade_size": 1,
            },
        },
        {
            "name": "__smoke__/ema_cross/SPY",
            "file_path": "strategies/example/ema_cross.py",
            "strategy_class": "EMACrossStrategy",
            "config_class": "EMACrossConfig",
            "default_config": {
                "instrument_id": "SPY.NASDAQ",
                "bar_type": "SPY.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 20,
                "trade_size": 1,
            },
        },
    ]
    async with portfolio_session_factory() as session:
        for row in rows:
            session.add(Strategy(**row))
        await session.commit()


@pytest.mark.asyncio
async def test_smoke_endpoint_creates_run_and_returns_201(
    api_client_authed,
    _seed_smoke_strategies: None,
    _mock_enqueue: None,
    _mock_ingest: None,
    _mock_smoke_redis: None,
) -> None:
    # Act
    response = await api_client_authed.post(
        "/api/v1/portfolios/smoke/runs?config=fast",
    )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["smoke"] is True
    assert body["status"], "status field should be non-empty"
    assert body["status"] in {"pending", "running"}


# ---------------------------------------------------------------------------
# Code-review iter-1 fix #4 — POST 201 must include a Location header
# pointing at the run-detail endpoint (api-design.md rule 4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_endpoint_returns_location_header_pointing_at_run_detail(
    api_client_authed,
    _seed_smoke_strategies: None,
    _mock_enqueue: None,
    _mock_ingest: None,
    _mock_smoke_redis: None,
) -> None:
    # Act
    response = await api_client_authed.post(
        "/api/v1/portfolios/smoke/runs?config=fast",
    )

    # Assert
    assert response.status_code == 201, response.text
    location = response.headers.get("Location")
    assert location is not None, "POST 201 must set Location header"
    body = response.json()
    assert location == f"/api/v1/portfolios/runs/{body['id']}"


# ---------------------------------------------------------------------------
# Code-review iter-1 fix #5 — IngestLockTimeoutError must surface as
# 409 with a structured INGEST_IN_PROGRESS body (NOT a generic 500).
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_ingest_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the smoke runner to raise IngestLockTimeoutError mid-flow."""
    from msai.services.smoke.ingest_lock import IngestLockTimeoutError

    async def _stub_acquire_raises(  # noqa: ANN401
        _pool: object,
        *,
        symbol: str,
        window_start: object,
        ttl_seconds: int,
        wait_timeout_seconds: int,
    ) -> str:
        raise IngestLockTimeoutError(f"Could not acquire ingest lock for {symbol} (forced in test)")

    monkeypatch.setattr("msai.services.smoke.runner.acquire_ingest_lock", _stub_acquire_raises)


@pytest.mark.asyncio
async def test_smoke_endpoint_returns_409_on_ingest_lock_timeout(
    api_client_authed,
    _seed_smoke_strategies: None,
    _mock_enqueue: None,
    _mock_ingest: None,
    _mock_smoke_redis: None,
    _mock_ingest_lock_timeout: None,
) -> None:
    # Act
    response = await api_client_authed.post(
        "/api/v1/portfolios/smoke/runs?config=fast",
    )

    # Assert
    assert response.status_code == 409, response.text
    body = response.json()
    # FastAPI wraps `detail` dicts as-is on HTTPException(detail=dict).
    detail = body["detail"]
    assert detail["code"] == "INGEST_IN_PROGRESS", detail
    assert "retry shortly" in detail["message"].lower(), detail


# ---------------------------------------------------------------------------
# Test-analyzer P0 fix #7 — route-ordering regression test
#
# /smoke/runs is a STATIC sub-path of the dynamic /{portfolio_id}/runs route.
# FastAPI resolves routes in declaration order; if the dynamic route is
# declared first, FastAPI tries to bind ``"smoke"`` as a UUID and emits 422,
# which masks the canonical-smoke endpoint entirely. Pin the declaration
# order with a regression test that posts both URLs in one test invocation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_static_route_is_declared_before_dynamic_uuid_route(
    api_client_authed,
    _seed_smoke_strategies: None,
    _mock_enqueue: None,
    _mock_ingest: None,
    _mock_smoke_redis: None,
) -> None:
    """The /smoke/runs static route must win over /{portfolio_id}/runs.

    Two POSTs in one test:
      1. POST /api/v1/portfolios/00000000-0000-0000-0000-000000000123/runs
         — a UUID that's not in the DB. The dynamic route fires and the
         service raises ValueError -> 404 (or 422 if FastAPI accepts the
         UUID syntactically but the lifecycle rejects the missing portfolio).
      2. POST /api/v1/portfolios/smoke/runs — the static route fires and
         returns 201 with smoke=True.

    If the static route were declared AFTER the dynamic route, step 2 would
    return 422 ("Input should be a valid UUID") because FastAPI would have
    tried to parse "smoke" as a UUID path parameter.
    """
    bogus_uuid = "00000000-0000-0000-0000-000000000123"

    # Step 1 — dynamic route fires for a non-existent UUID portfolio_id.
    dyn = await api_client_authed.post(
        f"/api/v1/portfolios/{bogus_uuid}/runs",
        json={
            "start_date": "2024-12-01",
            "end_date": "2024-12-31",
            "smoke": False,
        },
    )
    assert dyn.status_code in {404, 422}, (
        f"Expected 404/422 on unknown portfolio UUID, got {dyn.status_code}: {dyn.text}"
    )

    # Step 2 — static /smoke/runs must still resolve to the smoke endpoint.
    smoke = await api_client_authed.post(
        "/api/v1/portfolios/smoke/runs?config=fast",
    )
    assert smoke.status_code == 201, (
        f"Static /smoke/runs route did not win route ordering — got "
        f"{smoke.status_code}: {smoke.text}"
    )
    assert smoke.json()["smoke"] is True
