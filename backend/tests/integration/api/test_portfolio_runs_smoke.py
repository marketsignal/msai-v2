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
