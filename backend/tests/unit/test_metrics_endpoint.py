"""FastAPI /metrics endpoint tests (Phase 4 task 4.6).

The endpoint is a thin wrapper around ``get_registry().render()``,
so these tests verify the HTTP wiring: content type, status,
and that metrics written BEFORE the scrape show up in the
response body.
"""

from __future__ import annotations

import httpx
import pytest

from msai.main import app
from msai.services.observability import get_registry


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Each test gets a clean registry — metrics from a prior
    test must NOT bleed into this test's assertions."""
    get_registry().reset()
    yield
    get_registry().reset()


@pytest.fixture
def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class TestMetricsEndpoint:
    async def test_metrics_returns_200(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_metrics_content_type_is_prometheus_text(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        content_type = response.headers["content-type"]
        assert "text/plain" in content_type
        assert "version=0.0.4" in content_type

    async def test_metrics_body_contains_registered_counter(
        self, client: httpx.AsyncClient
    ) -> None:
        """Any counter written to the shared registry before
        the scrape must show up in the response body."""
        counter = get_registry().counter("msai_test_orders_total", "Test orders counter.")
        counter.inc()
        counter.inc()

        response = await client.get("/metrics")

        body = response.text
        assert "# HELP msai_test_orders_total Test orders counter." in body
        assert "# TYPE msai_test_orders_total counter" in body
        assert "msai_test_orders_total 2.0" in body

    async def test_metrics_body_contains_registered_gauge(self, client: httpx.AsyncClient) -> None:
        gauge = get_registry().gauge("msai_test_active", "Active test deployments.")
        gauge.set(7)

        response = await client.get("/metrics")

        body = response.text
        assert "msai_test_active 7.0" in body

    async def test_metrics_body_is_empty_when_no_metrics_registered(
        self, client: httpx.AsyncClient
    ) -> None:
        """An empty registry should still return 200 with a
        valid (empty-ish) body so Prometheus doesn't mark the
        target as broken before metrics are registered."""
        response = await client.get("/metrics")
        assert response.status_code == 200
        # Body may be empty or contain just a trailing newline —
        # both are valid Prometheus scrapes.
        assert response.text in ("", "\n")
