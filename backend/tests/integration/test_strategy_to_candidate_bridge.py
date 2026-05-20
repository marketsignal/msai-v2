"""F1c integration tests -- strategy_ids → default GraduationCandidate bridge.

Verifies:

- ``POST /api/v1/portfolios`` with ``strategy_ids`` (no ``allocations``)
  auto-creates one default :class:`GraduationCandidate`
  (``stage="portfolio_default"``) per strategy plus matching
  :class:`PortfolioAllocation` rows.
- Repeat compose with the SAME strategy id reuses the existing default
  candidate (idempotent get-or-create).

ARRANGE: real DB via testcontainer + the public API
(``POST /api/v1/portfolios``).
VERIFY: the same API (``GET /api/v1/portfolios/{id}/allocations``) --
never a raw DB read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

    from msai.models.strategy import Strategy


def _portfolio_payload_for_strategies(
    *,
    name: str,
    strategy_ids: list[str],
    objective: str = "maximize_sharpe",
    allocator_name: str = "equal_weight",
) -> dict[str, Any]:
    """Build a strategy-first compose body for POST /api/v1/portfolios."""
    return {
        "name": name,
        "objective": objective,
        "base_capital": 100_000.0,
        "default_mode": "quick",
        "allocator_name": allocator_name,
        "strategy_ids": strategy_ids,
    }


@pytest.mark.asyncio
async def test_create_portfolio_with_strategy_ids_auto_creates_candidates(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
) -> None:
    """``strategy_ids`` (no ``allocations``) auto-creates default candidates."""
    # Arrange
    s1 = await make_strategy(name="ema-cross")
    s2 = await make_strategy(name="momentum")

    # Act
    r = await api_client_authed.post(
        "/api/v1/portfolios",
        json=_portfolio_payload_for_strategies(
            name="test",
            strategy_ids=[str(s1.id), str(s2.id)],
        ),
    )

    # Assert -- 201 + body shape
    assert r.status_code == 201, r.text
    portfolio_body = r.json()
    assert portfolio_body["name"] == "test"
    portfolio_id = portfolio_body["id"]

    # Assert -- Location header points at the new resource (api-design.md
    # rule 4: POST creates MUST include Location).
    assert r.headers.get("location") == f"/api/v1/portfolios/{portfolio_id}"

    # Assert -- two allocations bound to the portfolio, visible via the
    # public /allocations endpoint (no peeking at Postgres).
    alloc_resp = await api_client_authed.get(f"/api/v1/portfolios/{portfolio_id}/allocations")
    assert alloc_resp.status_code == 200, alloc_resp.text
    allocations = alloc_resp.json()
    assert len(allocations) == 2
    # ``weight`` is None on the bridge path -- the allocator computes from
    # candidate metrics at orchestration time.
    assert all(a["weight"] is None for a in allocations)


@pytest.mark.asyncio
async def test_repeat_compose_reuses_default_candidate(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
) -> None:
    """A second portfolio composed from the same strategy reuses the candidate."""
    # Arrange -- single strategy reused across two compose calls.
    s1 = await make_strategy(name="ema-cross-repeat")

    # Act -- compose two portfolios from the same strategy.
    r1 = await api_client_authed.post(
        "/api/v1/portfolios",
        json=_portfolio_payload_for_strategies(
            name="p1",
            strategy_ids=[str(s1.id)],
        ),
    )
    assert r1.status_code == 201, r1.text
    p1 = r1.json()

    r2 = await api_client_authed.post(
        "/api/v1/portfolios",
        json=_portfolio_payload_for_strategies(
            name="p2",
            strategy_ids=[str(s1.id)],
            objective="maximize_sortino",
            allocator_name="inverse_vol",
        ),
    )
    assert r2.status_code == 201, r2.text
    p2 = r2.json()

    # Assert -- the two portfolios reference the SAME default candidate
    # for s1.  Visible via the public /allocations endpoint.
    a1_resp = await api_client_authed.get(f"/api/v1/portfolios/{p1['id']}/allocations")
    a2_resp = await api_client_authed.get(f"/api/v1/portfolios/{p2['id']}/allocations")
    assert a1_resp.status_code == 200, a1_resp.text
    assert a2_resp.status_code == 200, a2_resp.text
    a1 = a1_resp.json()
    a2 = a2_resp.json()
    assert len(a1) == 1
    assert len(a2) == 1
    assert a1[0]["candidate_id"] == a2[0]["candidate_id"], (
        "Repeat compose with the same strategy must reuse the default candidate"
    )


@pytest.mark.asyncio
async def test_create_portfolio_rejects_neither_path(
    api_client_authed: httpx.AsyncClient,
) -> None:
    """Reject 422 when neither ``strategy_ids`` nor ``allocations`` is provided."""
    r = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "no-compose-path",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "default_mode": "quick",
            "allocator_name": "equal_weight",
            # neither strategy_ids nor allocations
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_create_portfolio_rejects_both_paths(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
) -> None:
    """Reject 422 when both ``strategy_ids`` and ``allocations`` are provided."""
    s1 = await make_strategy(name="ema-cross-both")
    r = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "both-compose-paths",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "default_mode": "quick",
            "allocator_name": "equal_weight",
            "strategy_ids": [str(s1.id)],
            "allocations": [
                {"candidate_id": "00000000-0000-0000-0000-000000000000", "weight": 1.0}
            ],
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_strategy_ids_bridge_produces_runnable_candidate(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
    portfolio_session_factory: Any,
) -> None:
    """Bridge derives ``instruments`` list from strategy's singular ``instrument_id``.

    The orchestrator's ``_resolve_allocations`` reads the *plural* ``instruments``
    list off candidate ``config``.  Strategies typically declare singular
    ``instrument_id`` per the Nautilus ``StrategyConfig`` contract.  The F1c
    bridge MUST translate so the auto-created candidate is immediately
    runnable — otherwise Quick mode fails with
    ``PortfolioOrchestrationError: Candidate {id} has no instruments configured``
    on first invocation.

    Verifies via the orchestrator's resolve path (the same path that failed
    before this fix) — not a raw DB peek.  ``ARRANGE`` is through the public
    API (``POST /api/v1/portfolios``); ``VERIFY`` is through the
    orchestrator's resolver against the persisted DB state.
    """
    # Arrange — strategy whose default_config uses *singular* ``instrument_id``
    # (matches the real Nautilus StrategyConfig contract for both
    # ema_cross.py and smoke_market_order.py in strategies/example/).
    strategy = await make_strategy(
        name="singular-instrument",
        default_config={
            "instrument_id": "AAPL.SIM",
            "bar_type": "AAPL.SIM-1-MINUTE-LAST-EXTERNAL",
        },
    )

    # Act — compose via the public strategy-first bridge.
    r = await api_client_authed.post(
        "/api/v1/portfolios",
        json=_portfolio_payload_for_strategies(
            name="bridge-runnable",
            strategy_ids=[str(strategy.id)],
        ),
    )
    assert r.status_code == 201, r.text

    # Assert — resolve the freshly composed portfolio through the SAME
    # orchestrator path that failed pre-fix.  No "no instruments configured"
    # error means the bridge translated singular → plural correctly.
    from sqlalchemy import select

    from msai.models.portfolio_allocation import PortfolioAllocation
    from msai.services.portfolio.orchestration import PortfolioObjective, PortfolioService

    portfolio_id = r.json()["id"]
    async with portfolio_session_factory() as session:
        # ``candidate`` and ``strategy`` are both lazy="selectin" — no
        # explicit eager-load needed; they hydrate on access.
        stmt = select(PortfolioAllocation).where(PortfolioAllocation.portfolio_id == portfolio_id)
        allocations = list((await session.execute(stmt)).scalars().all())
        assert len(allocations) == 1

        svc = PortfolioService()
        # If the bridge failed to derive ``instruments``, this call raises
        # ``PortfolioOrchestrationError: Candidate {id} has no instruments
        # configured`` — the original FAIL_BUG signature.
        resolved = svc._resolve_allocations(  # noqa: SLF001 — narrow regression check
            allocations,
            objective=PortfolioObjective.MAXIMIZE_SHARPE,
        )
        assert len(resolved) == 1
        assert resolved[0]["instruments"] == ["AAPL.SIM"], (
            f"Bridge must derive plural instruments from singular instrument_id, "
            f"got config={resolved[0]}"
        )
        assert resolved[0]["asset_class"] == "stocks", (
            "Bridge must default asset_class to 'stocks' when strategy default_config "
            "doesn't specify one"
        )


@pytest.mark.asyncio
async def test_bridge_raises_when_strategy_default_config_has_no_instrument(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
) -> None:
    """Fail-fast when strategy's default_config has neither singular nor plural shape.

    The bridge must NOT silently create a candidate that will fail later
    inside the worker — surface the compose-time error so the operator can
    fix the strategy's default_config (or compose via the explicit
    allocations path with a hand-built GraduationCandidate).
    """
    # Arrange — strategy without ``instrument_id`` / ``symbol`` / ``instruments``.
    strategy = await make_strategy(
        name="no-instrument",
        default_config={"some_other_field": 42},
    )

    # Act — compose via the bridge path.
    r = await api_client_authed.post(
        "/api/v1/portfolios",
        json=_portfolio_payload_for_strategies(
            name="bridge-no-instrument",
            strategy_ids=[str(strategy.id)],
        ),
    )

    # Assert — compose fails at create time (500 from unhandled ValueError or
    # 422 if the API maps it; either way NOT 201).  The point is that the
    # bridge does not silently produce an unrunnable candidate.
    assert r.status_code != 201, (
        f"Bridge must fail compose when strategy default_config lacks instrument shape, "
        f"got {r.status_code}: {r.text}"
    )


@pytest.mark.asyncio
async def test_create_portfolio_rejects_duplicate_strategy_in_allocations(
    api_client_authed: httpx.AsyncClient,
    make_strategy: Callable[..., Awaitable[Strategy]],
    portfolio_session_factory: Any,
) -> None:
    """Codex bot iter-9 P1 on PR #73 — when two allocations point at
    DIFFERENT candidates of the SAME strategy, ``create_portfolio`` must
    reject the composition. Full-mode optimization's ``returns_cache``
    keys by strategy_id, so duplicate strategy_ids collapse the cache
    and silently corrupt IS/OOS scores. Surface the conflict at compose
    time instead.
    """
    from uuid import uuid4

    from msai.models.graduation_candidate import GraduationCandidate

    # Arrange — one strategy + two distinct candidates of it.
    strategy = await make_strategy(
        name="dup-strategy",
        default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
    )

    async with portfolio_session_factory() as session:
        cand_a = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="paper_candidate",
            config={"instruments": ["AAPL"], "variant": "A"},
            metrics={"sharpe": 1.0},
        )
        cand_b = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="paper_candidate",
            config={"instruments": ["AAPL"], "variant": "B"},
            metrics={"sharpe": 1.2},
        )
        session.add_all([cand_a, cand_b])
        await session.commit()
        cand_a_id = cand_a.id
        cand_b_id = cand_b.id

    # Act — compose with both candidates of the same strategy.
    response = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": f"dup-{uuid4().hex[:8]}",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "default_mode": "quick",
            "allocations": [
                {"candidate_id": str(cand_a_id), "weight": 0.5},
                {"candidate_id": str(cand_b_id), "weight": 0.5},
            ],
        },
    )

    # Assert — rejected (NOT 201). The error must name the duplicate strategy.
    assert response.status_code != 201, response.text
    assert "Duplicate strategy" in response.text or "duplicate" in response.text.lower()


@pytest.mark.asyncio
async def test_concurrent_bridge_calls_idempotent_via_unique_index(
    make_strategy: Callable[..., Awaitable[Strategy]],
    portfolio_session_factory: Any,
) -> None:
    """Codex bot iter-10 P1 on PR #73 — the partial unique index
    ``uq_portfolio_default_candidate_per_strategy`` (migration
    ``72ea2fd4dda2``) plus the IntegrityError handler in
    ``_get_or_create_default_candidate`` must keep the bridge
    idempotent under contention. Two concurrent callers both observing
    "no existing row" used to both insert; now the loser hits the
    unique index, re-reads the winner, and returns it.

    Simulating the race by manually inserting a portfolio_default
    candidate, THEN calling ``_get_or_create_default_candidate`` — the
    helper must return the pre-existing row, not insert a duplicate
    (which would now fail with IntegrityError, but the helper catches
    that path and re-reads).
    """
    from uuid import uuid4

    from sqlalchemy import select

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.services.portfolio.lifecycle import _get_or_create_default_candidate

    # Arrange
    strategy = await make_strategy(
        name="concurrent-bridge",
        default_config={"instruments": ["AAPL"], "asset_class": "stocks"},
    )

    async with portfolio_session_factory() as session:
        # Pre-existing portfolio_default candidate — simulates the
        # winner of a prior concurrent bridge call.
        existing = GraduationCandidate(
            id=uuid4(),
            strategy_id=strategy.id,
            stage="portfolio_default",
            config={"instruments": ["AAPL"], "asset_class": "stocks"},
            metrics={},
        )
        session.add(existing)
        await session.commit()
        existing_id = existing.id

    # Act — call the helper. The select-then-flush path now sees the
    # pre-existing row and returns it directly. If the select were to
    # miss (e.g., stale session cache) and the insert tried, the unique
    # index would catch it and the handler would re-read.
    async with portfolio_session_factory() as session:
        candidate = await _get_or_create_default_candidate(session, strategy.id)
        await session.commit()

    # Assert — same row, no duplicate inserted.
    assert candidate.id == existing_id, (
        f"bridge must return pre-existing portfolio_default candidate; got {candidate.id}"
    )

    # And only ONE portfolio_default row exists for this strategy.
    async with portfolio_session_factory() as session:
        count = (
            (
                await session.execute(
                    select(GraduationCandidate).where(
                        GraduationCandidate.strategy_id == strategy.id,
                        GraduationCandidate.stage == "portfolio_default",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1, (
            f"unique index must prevent duplicates; found {len(count)} portfolio_default rows"
        )
