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

    def test_histogram_empty_buckets_renders_only_inf(self) -> None:
        """An empty ``buckets=()`` tuple is legal — the histogram
        renders cleanly with only the mandatory ``+Inf`` bucket,
        ``_sum``, and ``_count`` lines, no finite-bucket sample lines."""
        registry = MetricsRegistry()
        hist = registry.histogram("msai_empty_hist", "Empty buckets.", buckets=())
        hist.observe(42)
        hist.observe(100)

        text = registry.render()
        assert 'msai_empty_hist_bucket{le="+Inf"} 2' in text
        assert "msai_empty_hist_sum 142.0" in text
        assert "msai_empty_hist_count 2" in text
        # No finite-bucket lines should appear.
        lines = [line for line in text.split("\n") if "msai_empty_hist_bucket" in line]
        assert len(lines) == 1
        assert '{le="+Inf"}' in lines[0]

    def test_histogram_negative_observation_lands_in_all_buckets(self) -> None:
        """Negative observations are cumulative like any other: they
        satisfy ``value <= upper`` for every bucket, including +Inf."""
        registry = MetricsRegistry()
        hist = registry.histogram("msai_neg_hist", "Neg obs.", buckets=(100,))
        hist.observe(-5)

        text = registry.render()
        assert 'msai_neg_hist_bucket{le="100"} 1' in text
        assert 'msai_neg_hist_bucket{le="+Inf"} 1' in text
        assert "msai_neg_hist_count 1" in text
        assert "msai_neg_hist_sum -5.0" in text

    def test_histogram_rejects_nan_observation(self) -> None:
        """NaN must raise — ``float('nan') <= x`` is False for every
        bucket including +Inf, which would break the Prometheus
        invariant ``+Inf bucket count == _count``."""
        registry = MetricsRegistry()
        hist = registry.histogram("msai_nan_hist", "NaN test.", buckets=(100,))
        with pytest.raises(ValueError, match="rejected NaN"):
            hist.observe(float("nan"))

    def test_histogram_sum_matches_observed_values(self) -> None:
        """The rendered ``_sum`` line must equal the exact sum of
        observations (float representation)."""
        registry = MetricsRegistry()
        hist = registry.histogram("msai_sum_hist", "Sum test.", buckets=(1_000,))
        values = [10, 25, 50, 125]
        for v in values:
            hist.observe(v)

        text = registry.render()
        expected_sum = float(sum(values))
        assert f"msai_sum_hist_sum {expected_sum}" in text
        assert "msai_sum_hist_count 4" in text

    def test_histogram_type_conflict_raises(self) -> None:
        """Registering the same name first as counter then as
        histogram is a programmer error — raise ``TypeError``
        rather than silently returning the wrong type."""
        registry = MetricsRegistry()
        registry.counter("msai_conflict_hist", "Conflict test.")
        with pytest.raises(TypeError, match="already registered"):
            registry.histogram("msai_conflict_hist", "Conflict test.", buckets=(1, 10))


class TestBacktestResultsPayloadBytesHistogram:
    """Canonical ``msai_backtest_results_payload_bytes`` histogram.

    Verifies the pre-registered singleton in ``trading_metrics`` renders
    with the expected bucket layout + name so the worker and API
    observation sites can trust the global instance. Tests against the
    public ``render()`` contract — never against private ``_count`` /
    ``_buckets`` attrs.
    """

    def test_backtest_results_payload_bytes_histogram_defined(self) -> None:
        from msai.services.observability.trading_metrics import (
            msai_backtest_results_payload_bytes,
        )

        assert msai_backtest_results_payload_bytes is not None

    def test_histogram_observe_shows_in_render(self) -> None:
        from msai.services.observability.trading_metrics import (
            msai_backtest_results_payload_bytes,
        )

        msai_backtest_results_payload_bytes.observe(50_000)
        lines = msai_backtest_results_payload_bytes.render()
        text = "\n".join(lines)
        # Bucket bounds from the plan: 1KB / 10KB / 100KB / 1MB / 10MB.
        # A 50_000-byte observation must land in the 100KB bucket (102400).
        assert "msai_backtest_results_payload_bytes_bucket" in text
        assert 'le="102400"' in text
        assert "msai_backtest_results_payload_bytes_count" in text
        assert "msai_backtest_results_payload_bytes_sum" in text

    def test_histogram_registered_via_global_registry(self) -> None:
        # Importing trading_metrics triggers the pre-registration as a
        # side effect. Re-import inside the test so the assertion on the
        # rendered HELP/TYPE lines is deterministic regardless of the
        # order in which other tests imported the module.
        from msai.services.observability import (
            get_registry,
            trading_metrics,  # noqa: F401
        )

        registry = get_registry()
        render_text = registry.render()
        assert "# HELP msai_backtest_results_payload_bytes" in render_text
        assert "# TYPE msai_backtest_results_payload_bytes histogram" in render_text


class TestBacktestTradesPageCounter:
    """``msai_backtest_trades_page_count`` counter.

    Labeled by ``page_size`` so operators can distinguish normal pagination
    (``page_size=100``) from clamped-at-ceiling abuse (``page_size=500``).
    """

    def test_counter_registered_via_global_registry(self) -> None:
        from msai.services.observability import (
            get_registry,
            trading_metrics,  # noqa: F401
        )

        registry = get_registry()
        render_text = registry.render()
        assert "# HELP msai_backtest_trades_page_count" in render_text
        assert "# TYPE msai_backtest_trades_page_count counter" in render_text

    def test_counter_labels_distinguish_page_sizes(self) -> None:
        from msai.services.observability.trading_metrics import (
            msai_backtest_trades_page_count,
        )

        msai_backtest_trades_page_count.labels(page_size="100").inc()
        msai_backtest_trades_page_count.labels(page_size="100").inc()
        msai_backtest_trades_page_count.labels(page_size="500").inc()

        text = "\n".join(msai_backtest_trades_page_count.render())
        # Distinct rows per label-value — verifies the counter isn't
        # silently sharing state across labels.
        assert 'page_size="100"' in text
        assert 'page_size="500"' in text
