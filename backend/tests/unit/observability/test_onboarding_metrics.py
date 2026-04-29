"""Tests for symbol onboarding observability metrics (T13)."""

from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import (
    onboarding_ib_timeout_total,
    onboarding_jobs_total,
    onboarding_symbol_duration_seconds,
)


def test_onboarding_metrics_render() -> None:
    """Verify symbol onboarding metrics are registered and renderable."""
    onboarding_jobs_total.labels(status="completed").inc()
    onboarding_symbol_duration_seconds.observe(12.3)
    onboarding_ib_timeout_total.inc()
    rendered = get_registry().render()
    assert "msai_onboarding_jobs_total" in rendered
    assert "msai_onboarding_symbol_duration_seconds" in rendered
    assert "msai_onboarding_ib_timeout_total" in rendered
