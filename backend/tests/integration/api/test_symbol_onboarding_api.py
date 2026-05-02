"""Integration tests for POST /onboard, GET /status, POST /repair.

Verifies the council-pinned idempotency contract:
- enqueue-first-then-commit
- duplicate by digest -> 200 OK + existing run_id
- enqueue race (job is None) without committed row -> 409 DUPLICATE_IN_FLIGHT
- pool / enqueue raises -> 503 QUEUE_UNAVAILABLE, no row written

Uses ``httpx.AsyncClient`` + ``ASGITransport`` (NOT TestClient) so the
async DB engine and the app share the same event loop — TestClient
spawns a worker thread which causes "future attached to a different
loop" errors against an asyncpg engine created in the test's loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _build_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()

    async def _stub_user() -> dict[str, str]:
        return {"sub": "test-user", "email": "test@example.com"}

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_current_user] = _stub_user
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(symbol_onboarding_router)
    return app


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


_DEFAULT_JOB = object()  # sentinel — distinct from None so callers can request a None return.


def _make_pool(
    *, enqueue_returns: Any = _DEFAULT_JOB, raises: Exception | None = None
) -> MagicMock:
    pool = MagicMock()
    if raises is not None:
        pool.enqueue_job = AsyncMock(side_effect=raises)
    else:
        if enqueue_returns is _DEFAULT_JOB:
            job_obj = MagicMock()
            job_obj.job_id = "fake-job-id"
            return_value = job_obj
        else:
            return_value = enqueue_returns
        pool.enqueue_job = AsyncMock(return_value=return_value)
    pool.abort_job = AsyncMock(return_value=None)
    return pool


def _body(symbol: str = "SPY") -> dict[str, Any]:
    return {
        "watchlist_name": "core",
        "symbols": [
            {
                "symbol": symbol,
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
        "request_live_qualification": False,
    }


@pytest.mark.asyncio
async def test_post_onboard_returns_202_and_enqueues_task(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool()
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["watchlist_name"] == "core"
    assert "run_id" in data

    pool.enqueue_job.assert_awaited_once()
    call = pool.enqueue_job.await_args
    assert call.args[0] == "run_symbol_onboarding"
    assert call.kwargs["_queue_name"] == "msai:ingest"
    assert call.kwargs["_job_id"].startswith("symbol-onboarding:")

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].id) == data["run_id"]


@pytest.mark.asyncio
async def test_duplicate_submit_returns_200_with_existing_run_id(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool()
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        first = await client.post("/api/v1/symbols/onboard", json=_body())
        assert first.status_code == 202
        second = await client.post("/api/v1/symbols/onboard", json=_body())

    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    pool.enqueue_job.assert_awaited_once()

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_duplicate_submit_during_race_returns_409_when_row_not_visible_yet(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``enqueue_job`` returns ``None`` (arq dedup) and no committed row materialized."""
    pool = _make_pool(enqueue_returns=None)
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DUPLICATE_IN_FLIGHT"

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_redis_down_returns_503_and_commits_no_row(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool(raises=ConnectionError("redis down"))
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "QUEUE_UNAVAILABLE"

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 0


@pytest_asyncio.fixture
async def seeded_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> SymbolOnboardingRun:
    """Seed a SymbolOnboardingRun with mixed per-symbol states."""
    run = SymbolOnboardingRun(
        id=uuid4(),
        watchlist_name="core",
        status=SymbolOnboardingRunStatus.COMPLETED_WITH_FAILURES,
        symbol_states={
            "AAPL": {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "succeeded",
                "step": "completed",
                "error": None,
            },
            "BAD": {
                "symbol": "BAD",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "failed",
                "step": "bootstrap",
                "error": {"code": "BOOTSTRAP_AMBIGUOUS", "message": "ambiguous"},
            },
            "ZIN": {
                "symbol": "ZIN",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "in_progress",
                "step": "ingest",
                "error": None,
            },
            "WAITING": {
                "symbol": "WAITING",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "not_started",
                "step": "pending",
                "error": None,
            },
        },
        request_live_qualification=False,
        cost_ceiling_usd=None,
        job_id_digest="seeded-digest-test",
    )
    async with session_factory() as s:
        s.add(run)
        await s.commit()
        await s.refresh(run)
    return run


@pytest.mark.asyncio
async def test_get_status_returns_progress_counts(
    client: httpx.AsyncClient,
    seeded_run: SymbolOnboardingRun,
) -> None:
    resp = await client.get(f"/api/v1/symbols/onboard/{seeded_run.id}/status")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed_with_failures"
    progress = data["progress"]
    assert progress == {
        "total": 4,
        "succeeded": 1,
        "failed": 1,
        "in_progress": 1,
        "not_started": 1,
    }
    by_symbol = {row["symbol"]: row for row in data["per_symbol"]}
    assert by_symbol["BAD"]["next_action"] is not None
    assert by_symbol["AAPL"]["next_action"] is None


@pytest.mark.asyncio
async def test_get_status_404_for_unknown_run(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(f"/api/v1/symbols/onboard/{uuid4()}/status")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_post_repair_rejects_in_progress_parent(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    parent = SymbolOnboardingRun(
        id=uuid4(),
        watchlist_name="core",
        status=SymbolOnboardingRunStatus.IN_PROGRESS,
        symbol_states={
            "X": {
                "symbol": "X",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "failed",
                "step": "bootstrap",
                "error": {"code": "INGEST_FAILED", "message": "x"},
            }
        },
        request_live_qualification=False,
        cost_ceiling_usd=None,
        job_id_digest="parent-in-progress",
    )
    async with session_factory() as s:
        s.add(parent)
        await s.commit()
        await s.refresh(parent)

    resp = await client.post(f"/api/v1/symbols/onboard/{parent.id}/repair", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PARENT_RUN_IN_PROGRESS"


# ---------------------------------------------------------------------------
# B5 — cost-cap fallback gated on databento_api_key presence
# ---------------------------------------------------------------------------


def _fake_estimate(total_usd: float) -> Any:
    """Minimal CostEstimate-shaped object for cap-fallback tests."""
    from msai.services.symbol_onboarding.cost_estimator import CostEstimate, CostLine

    return CostEstimate(
        total_usd=total_usd,
        symbol_count=1,
        breakdown=[CostLine("AAPL", "equity", "XNAS.ITCH", total_usd)],
        confidence="high",
        basis="databento.metadata.get_cost (1m OHLCV)",
    )


@pytest.mark.asyncio
async def test_onboard_uses_settings_default_when_cap_omitted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request omitting cost_ceiling_usd should still be capped via settings default."""
    from decimal import Decimal

    from msai.core.config import settings

    monkeypatch.setattr(settings, "symbol_onboarding_default_cost_ceiling_usd", Decimal("0.01"))
    # Ensure the cap-enforced path runs (not the cap-skipped path).
    monkeypatch.setattr(settings, "databento_api_key", "test-key-present")

    payload = {
        "watchlist_name": "test-cap-fallback",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        # cost_ceiling_usd intentionally omitted
    }

    pool = _make_pool()
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "msai.api.symbol_onboarding.estimate_cost",
            new_callable=AsyncMock,
            return_value=_fake_estimate(total_usd=5.00),
        ),
        patch(
            "msai.api.symbol_onboarding._get_databento_client",
            return_value=object(),
        ),
    ):
        response = await client.post("/api/v1/symbols/onboard", json=payload)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "COST_CEILING_EXCEEDED"
    # Estimate ($5.00) exceeds the patched $0.01 default cap → cap-fallback engaged.
    pool.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_onboard_request_cap_overrides_settings_default(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-request cost_ceiling_usd wins over settings default."""
    from decimal import Decimal

    from msai.core.config import settings

    monkeypatch.setattr(settings, "symbol_onboarding_default_cost_ceiling_usd", Decimal("0.01"))
    monkeypatch.setattr(settings, "databento_api_key", "test-key-present")

    payload = {
        "watchlist_name": "test-cap-override",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        "cost_ceiling_usd": "100.00",  # well above any realistic estimate
    }

    pool = _make_pool()
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "msai.api.symbol_onboarding.estimate_cost",
            new_callable=AsyncMock,
            return_value=_fake_estimate(total_usd=5.00),
        ),
        patch(
            "msai.api.symbol_onboarding._get_databento_client",
            return_value=object(),
        ),
    ):
        response = await client.post("/api/v1/symbols/onboard", json=payload)

    assert response.status_code == 202, response.text
    pool.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_skips_cap_when_databento_key_absent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATABENTO_API_KEY is unset, omit the cap-check (worker would fail anyway)."""
    from msai.core.config import settings

    monkeypatch.setattr(settings, "databento_api_key", "")

    payload = {
        "watchlist_name": "test-no-key",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        # cost_ceiling_usd omitted
    }

    pool = _make_pool()
    # estimate_cost MUST NOT be called when the cap-skipped path is taken; assert
    # via a side_effect that would fail loudly if invoked.
    estimate_mock = AsyncMock(side_effect=AssertionError("estimate_cost should not be called"))
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch("msai.api.symbol_onboarding.estimate_cost", new=estimate_mock),
    ):
        response = await client.post("/api/v1/symbols/onboard", json=payload)

    assert response.status_code == 202, response.text
    estimate_mock.assert_not_awaited()
    pool.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_clears_hidden_from_inventory_pre_dedup(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override O-11 race fix: POST /onboard clears hidden_from_inventory=False
    before any dedup-check runs, so re-onboarding a removed symbol restores
    visibility even if the run is deduplicated.
    """
    from datetime import date

    from sqlalchemy import select

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    _ = date  # keep ruff happy (used below in `effective_from=date(...)`)

    # Seed a hidden instrument directly in the DB
    async with session_factory() as s:
        defn = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="XNAS",
            routing_venue="XNAS",
            asset_class="equity",
            provider="databento",
            hidden_from_inventory=True,  # already removed
        )
        s.add(defn)
        await s.flush()
        s.add(
            InstrumentAlias(
                instrument_uid=defn.instrument_uid,
                alias_string="AAPL.XNAS",
                provider="databento",
                venue_format="exchange_name",
                effective_from=date(2020, 1, 1),
                effective_to=None,
            )
        )
        await s.commit()
        instrument_uid = defn.instrument_uid

    payload = {
        "watchlist_name": "test-restore",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        "cost_ceiling_usd": "100.00",
    }
    from msai.core.config import settings

    monkeypatch.setattr(settings, "databento_api_key", "test-key-present")

    pool = _make_pool()
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "msai.api.symbol_onboarding.estimate_cost",
            new_callable=AsyncMock,
            return_value=_fake_estimate(total_usd=5.00),
        ),
        patch(
            "msai.api.symbol_onboarding._get_databento_client",
            return_value=object(),
        ),
    ):
        response = await client.post("/api/v1/symbols/onboard", json=payload)
    assert response.status_code == 202, response.text

    # AAPL's hidden flag should now be False
    async with session_factory() as s:
        result = await s.execute(
            select(InstrumentDefinition.hidden_from_inventory).where(
                InstrumentDefinition.instrument_uid == instrument_uid
            )
        )
        hidden = result.scalar_one()
    assert hidden is False, "POST /onboard should have cleared hidden_from_inventory pre-dedup"


@pytest.mark.asyncio
async def test_re_onboard_after_delete_restores_visibility_even_when_deduplicated(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override O-11 (iter-1 review reinforcement): the dedup path is the
    actual race we're guarding against — the prior committed onboarding run
    means the second POST hits ``_dedup_job_id`` → returns 200 with the
    existing ``run_id`` and DOES NOT call ``_enqueue_and_persist_run`` again.
    Without the explicit pre-dedup UPDATE, the ``hidden_from_inventory`` flag
    would never get cleared.
    """
    from datetime import date

    from sqlalchemy import select

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    # Step 1: seed a HIDDEN instrument that already has a COMMITTED prior run
    # matching the same canonical request shape — so step 3's POST will be
    # deduplicated rather than spawning a new run.
    payload: dict[str, Any] = {
        "watchlist_name": "test-restore-dedup",
        "symbols": [
            {
                "symbol": "MSFT",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        "cost_ceiling_usd": "100.00",
    }

    from msai.core.config import settings

    monkeypatch.setattr(settings, "databento_api_key", "test-key-present")

    pool = _make_pool()
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "msai.api.symbol_onboarding.estimate_cost",
            new_callable=AsyncMock,
            return_value=_fake_estimate(total_usd=5.00),
        ),
        patch(
            "msai.api.symbol_onboarding._get_databento_client",
            return_value=object(),
        ),
    ):
        # First POST: persists a SymbolOnboardingRun with this digest.
        first = await client.post("/api/v1/symbols/onboard", json=payload)
        assert first.status_code == 202, first.text
        first_run_id = first.json()["run_id"]

        # Soft-delete the alias so MSFT is now hidden.
        async with session_factory() as s:
            defn = InstrumentDefinition(
                raw_symbol="MSFT",
                listing_venue="XNAS",
                routing_venue="XNAS",
                asset_class="equity",
                provider="databento",
                hidden_from_inventory=True,
            )
            s.add(defn)
            await s.flush()
            s.add(
                InstrumentAlias(
                    instrument_uid=defn.instrument_uid,
                    alias_string="MSFT.XNAS",
                    provider="databento",
                    venue_format="exchange_name",
                    effective_from=date(2020, 1, 1),
                    effective_to=None,
                )
            )
            await s.commit()
            instrument_uid = defn.instrument_uid

        # Second POST with IDENTICAL canonical body — must dedup to first run.
        second = await client.post("/api/v1/symbols/onboard", json=payload)
    assert second.status_code in (200, 202), second.text
    assert second.json()["run_id"] == first_run_id, "second POST should dedup to first run"
    # Iter-2 review fix (P2-C): assert single enqueue_job call across both
    # POSTs. Proves the second hit took the dedup branch rather than spawning
    # a new arq job.
    pool.enqueue_job.assert_awaited_once()

    # Despite the dedup short-circuit, the pre-dedup UPDATE must have cleared
    # the hidden flag.
    async with session_factory() as s:
        result = await s.execute(
            select(InstrumentDefinition.hidden_from_inventory).where(
                InstrumentDefinition.instrument_uid == instrument_uid
            )
        )
        hidden = result.scalar_one()
    assert hidden is False, (
        "POST /onboard pre-dedup UPDATE must clear hidden_from_inventory "
        "even when the run is deduplicated."
    )


@pytest.mark.asyncio
async def test_worker_upsert_does_not_modify_hidden_from_inventory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Override O-15: ``_upsert_definition_and_alias`` (the writer path used
    by the worker / instrument-bootstrap on every onboard run) MUST NEVER
    set or clear ``hidden_from_inventory``. That column is exclusively
    user-owned (DELETE endpoint sets, POST handler pre-dedup clears).

    Race scenario this test guards: user removes AAPL while a prior onboard
    is in flight; worker eventually completes the prior run and calls into
    ``_upsert_definition_and_alias`` to refresh definition metadata. If that
    UPSERT touched ``hidden_from_inventory``, the user's intent (hide) would
    be silently overridden.
    """
    from datetime import date

    from sqlalchemy import select

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition
    from msai.services.nautilus.security_master.service import SecurityMaster

    # ARRANGE: seed an instrument with hidden_from_inventory=True (post-DELETE state)
    async with session_factory() as s:
        defn = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="XNAS",
            routing_venue="XNAS",
            asset_class="equity",
            provider="interactive_brokers",
            hidden_from_inventory=True,
        )
        s.add(defn)
        await s.flush()
        s.add(
            InstrumentAlias(
                instrument_uid=defn.instrument_uid,
                alias_string="AAPL.XNAS",
                provider="interactive_brokers",
                venue_format="exchange_name",
                effective_from=date(2020, 1, 1),
                effective_to=None,
            )
        )
        await s.commit()
        instrument_uid = defn.instrument_uid

    # ACT: invoke the worker's writer path with the SAME tuple — would refresh
    # the definition's venue/refreshed_at if anything had changed. This is
    # exactly what happens when an in-flight worker run completes after the
    # user has DELETEd the symbol.
    async with session_factory() as s:
        master = SecurityMaster(db=s)
        await master._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="XNAS",
            routing_venue="XNAS",
            asset_class="equity",
            alias_string="AAPL.XNAS",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await s.commit()

    # ASSERT: hidden_from_inventory still True — worker UPSERT must NOT touch it.
    async with session_factory() as s:
        result = await s.execute(
            select(InstrumentDefinition.hidden_from_inventory).where(
                InstrumentDefinition.instrument_uid == instrument_uid
            )
        )
        hidden = result.scalar_one()
    assert hidden is True, (
        "Worker's _upsert_definition_and_alias must NOT modify "
        "hidden_from_inventory — that column is user-owned (Override O-15)."
    )


@pytest.mark.asyncio
async def test_cost_cap_rejection_does_not_clear_hidden_from_inventory(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iter-1 review fix P2-1 regression test: a 422 ``COST_CEILING_EXCEEDED``
    rejection must NOT silently un-hide a soft-deleted symbol.

    Pre-fix bug: pre-dedup hidden-clear UPDATE ran BEFORE the cap-check; a
    user who removed AAPL then submitted an onboard above their ceiling
    would see AAPL re-appear in inventory with no run started. Post-fix,
    the clear runs AFTER the 422 short-circuit so a rejected onboard
    leaves user state intact.
    """
    from datetime import date

    from sqlalchemy import select

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    # ARRANGE: hidden alias for AAPL, ceiling lower than the dry-run estimate.
    async with session_factory() as s:
        defn = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="XNAS",
            routing_venue="XNAS",
            asset_class="equity",
            provider="databento",
            hidden_from_inventory=True,
        )
        s.add(defn)
        await s.flush()
        s.add(
            InstrumentAlias(
                instrument_uid=defn.instrument_uid,
                alias_string="AAPL.XNAS",
                provider="databento",
                venue_format="exchange_name",
                effective_from=date(2020, 1, 1),
                effective_to=None,
            )
        )
        await s.commit()
        instrument_uid = defn.instrument_uid

    payload: dict[str, Any] = {
        "watchlist_name": "test-cap-reject",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2025-01-01",
            }
        ],
        "cost_ceiling_usd": "1.00",  # below the fake estimate
    }

    from msai.core.config import settings

    monkeypatch.setattr(settings, "databento_api_key", "test-key-present")

    pool = _make_pool()
    with (
        patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "msai.api.symbol_onboarding.estimate_cost",
            new_callable=AsyncMock,
            return_value=_fake_estimate(total_usd=10.00),
        ),
        patch(
            "msai.api.symbol_onboarding._get_databento_client",
            return_value=object(),
        ),
    ):
        # ACT: cap=$1, estimate=$10 → 422
        response = await client.post("/api/v1/symbols/onboard", json=payload)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "COST_CEILING_EXCEEDED", body

    # ASSERT: hidden flag is still True — rejection must not change user state.
    async with session_factory() as s:
        result = await s.execute(
            select(InstrumentDefinition.hidden_from_inventory).where(
                InstrumentDefinition.instrument_uid == instrument_uid
            )
        )
        hidden = result.scalar_one()
    assert hidden is True, (
        "COST_CEILING_EXCEEDED 422 must NOT un-hide a soft-deleted symbol "
        "(iter-1 P2-1 fix: pre-dedup clear must run AFTER cap-check)."
    )
    # And no enqueue_job call was made — the rejection short-circuited.
    pool.enqueue_job.assert_not_awaited()
