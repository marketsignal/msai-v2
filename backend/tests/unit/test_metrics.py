"""Unit tests for the in-process metrics registry
(Phase 4 task 4.6).

We exercise the registry directly — the FastAPI ``/metrics``
endpoint is a thin wrapper that just calls ``render()``.
"""

from __future__ import annotations

import pytest

from msai.services.observability.metrics import (
    MetricsRegistry,
)


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


class TestCounter:
    def test_counter_starts_at_zero(self, registry: MetricsRegistry) -> None:
        registry.counter("msai_test_total", "A test counter.")
        rendered = registry.render()
        assert "msai_test_total 0" in rendered

    def test_counter_increments(self, registry: MetricsRegistry) -> None:
        counter = registry.counter("msai_test_total", "A test counter.")
        counter.inc()
        counter.inc()
        counter.inc()
        rendered = registry.render()
        assert "msai_test_total 3.0" in rendered

    def test_counter_inc_with_amount(self, registry: MetricsRegistry) -> None:
        counter = registry.counter("msai_test_total", "A test counter.")
        counter.inc(5.5)
        rendered = registry.render()
        assert "msai_test_total 5.5" in rendered

    def test_counter_cannot_decrement(self, registry: MetricsRegistry) -> None:
        counter = registry.counter("msai_test_total", "A test counter.")
        with pytest.raises(ValueError, match="cannot be decremented"):
            counter.inc(-1)

    def test_counter_with_labels(self, registry: MetricsRegistry) -> None:
        counter = registry.counter("msai_orders_total", "Orders grouped by status.")
        counter.inc(deployment_id="abc", status="filled")
        counter.inc(deployment_id="abc", status="filled")
        counter.inc(deployment_id="abc", status="rejected")

        rendered = registry.render()
        assert 'msai_orders_total{deployment_id="abc",status="filled"} 2.0' in rendered
        assert 'msai_orders_total{deployment_id="abc",status="rejected"} 1.0' in rendered

    def test_counter_labels_child_api(self, registry: MetricsRegistry) -> None:
        """The ``.labels()`` pattern matches the prometheus_client
        API — lets callers cache the label set."""
        counter = registry.counter("msai_fills_total", "Fills.")
        child = counter.labels(deployment_id="xyz")
        child.inc()
        child.inc()

        rendered = registry.render()
        assert 'msai_fills_total{deployment_id="xyz"} 2.0' in rendered


class TestGauge:
    def test_gauge_set(self, registry: MetricsRegistry) -> None:
        gauge = registry.gauge("msai_active_deployments", "Active count.")
        gauge.set(5)
        rendered = registry.render()
        assert "msai_active_deployments 5.0" in rendered

    def test_gauge_inc_and_dec(self, registry: MetricsRegistry) -> None:
        gauge = registry.gauge("msai_active_deployments", "Active count.")
        gauge.inc()
        gauge.inc()
        gauge.inc()
        gauge.dec()
        rendered = registry.render()
        assert "msai_active_deployments 2.0" in rendered

    def test_gauge_with_labels(self, registry: MetricsRegistry) -> None:
        gauge = registry.gauge("msai_positions", "Position count per deployment.")
        gauge.set(3, deployment_id="alpha")
        gauge.set(7, deployment_id="beta")

        rendered = registry.render()
        assert 'msai_positions{deployment_id="alpha"} 3.0' in rendered
        assert 'msai_positions{deployment_id="beta"} 7.0' in rendered

    def test_gauge_labels_child_api(self, registry: MetricsRegistry) -> None:
        gauge = registry.gauge("msai_ib_connected", "IB connection status.")
        gauge.labels(deployment_id="z").set(1)
        rendered = registry.render()
        assert 'msai_ib_connected{deployment_id="z"} 1.0' in rendered


class TestRegistry:
    def test_idempotent_counter_registration(self, registry: MetricsRegistry) -> None:
        """Calling ``counter(name)`` twice with the same name
        must return the SAME instance so module-level callers
        don't have to coordinate."""
        first = registry.counter("msai_dedup_total", "Test.")
        second = registry.counter("msai_dedup_total", "Test.")
        assert first is second

    def test_idempotent_gauge_registration(self, registry: MetricsRegistry) -> None:
        first = registry.gauge("msai_dedup_gauge", "Test.")
        second = registry.gauge("msai_dedup_gauge", "Test.")
        assert first is second

    def test_type_conflict_raises(self, registry: MetricsRegistry) -> None:
        """Registering the same name as both a counter and a
        gauge is a programmer error — raise loudly rather than
        silently returning the wrong type."""
        registry.counter("msai_conflict", "Test.")
        with pytest.raises(TypeError, match="already registered"):
            registry.gauge("msai_conflict", "Test.")

    def test_render_format_is_valid_prometheus(self, registry: MetricsRegistry) -> None:
        """Smoke test: the rendered body is parseable as the
        Prometheus text format (lines alternate # HELP, # TYPE,
        and sample lines)."""
        counter = registry.counter("msai_a_total", "Counter A.")
        counter.inc()
        gauge = registry.gauge("msai_b", "Gauge B.")
        gauge.set(42)

        rendered = registry.render()
        lines = rendered.strip().split("\n")

        # Counter metric section
        assert "# HELP msai_a_total Counter A." in lines
        assert "# TYPE msai_a_total counter" in lines
        # Gauge metric section
        assert "# HELP msai_b Gauge B." in lines
        assert "# TYPE msai_b gauge" in lines

    def test_render_sorts_metrics_by_name(self, registry: MetricsRegistry) -> None:
        """Deterministic rendering — helpful for diff-based
        assertions in tests and for consistent scrape output."""
        registry.counter("msai_z_total", "Z.")
        registry.counter("msai_a_total", "A.")
        registry.counter("msai_m_total", "M.")

        rendered = registry.render()
        # a appears before m appears before z
        a_index = rendered.index("msai_a_total")
        m_index = rendered.index("msai_m_total")
        z_index = rendered.index("msai_z_total")
        assert a_index < m_index < z_index

    def test_reset_clears_everything(self, registry: MetricsRegistry) -> None:
        registry.counter("msai_x_total", "X.").inc()
        registry.reset()
        rendered = registry.render()
        assert "msai_x_total" not in rendered

    def test_label_values_are_escaped(self, registry: MetricsRegistry) -> None:
        """Backslash, double-quote, and newline must be escaped
        per the Prometheus text format spec."""
        counter = registry.counter("msai_escape_total", "Escape test.")
        counter.inc(path='/bad"value\\with\nnewline')
        rendered = registry.render()
        # Check escaped sequences are in the output
        assert '\\"' in rendered  # escaped double-quote
        assert "\\\\" in rendered  # escaped backslash
        assert "\\n" in rendered  # escaped newline


class TestHistogram:
    def test_histogram_records_observations_and_buckets(self) -> None:
        from msai.services.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        hist = registry.histogram(
            "msai_test_hist",
            "Test histogram.",
            buckets=(100, 1_000, 10_000),
        )
        hist.observe(50)
        hist.observe(500)
        hist.observe(5_000)
        hist.observe(50_000)

        text = registry.render()
        assert "msai_test_hist_bucket" in text
        assert "msai_test_hist_sum" in text
        assert "msai_test_hist_count" in text
        # 50 → le=100; 500 → le=1000; 5000 → le=10000; 50000 only le=+Inf
        assert 'msai_test_hist_bucket{le="100"} 1' in text
        assert 'msai_test_hist_bucket{le="1000"} 2' in text
        assert 'msai_test_hist_bucket{le="10000"} 3' in text
        assert 'msai_test_hist_bucket{le="+Inf"} 4' in text
        assert "msai_test_hist_count 4" in text

    def test_histogram_idempotent_registration(self) -> None:
        from msai.services.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        h1 = registry.histogram("msai_dup", "dup", buckets=(1, 10))
        h2 = registry.histogram("msai_dup", "dup", buckets=(1, 10))
        assert h1 is h2
