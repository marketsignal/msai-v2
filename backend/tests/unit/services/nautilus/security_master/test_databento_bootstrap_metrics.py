"""Verify observability: DATABENTO_API_CALLS_TOTAL, REGISTRY_BOOTSTRAP_TOTAL,
REGISTRY_BOOTSTRAP_DURATION_MS emit with correct labels."""

from __future__ import annotations

import pytest

from msai.services.nautilus.security_master.databento_bootstrap import (
    DatabentoBootstrapService,
)
from msai.services.observability import get_registry

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_bootstrap_emits_counter_and_histogram(session_factory, mock_databento):
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
        max_concurrent=3,
    )
    await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)

    rendered = get_registry().render()
    # Counter: labels are sorted alphabetically per metrics.py:61 _format_labels()
    # → asset_class, outcome, provider
    assert (
        'msai_registry_bootstrap_total{asset_class="equity",outcome="created",provider="databento"}'
        in rendered
    )
    # Histogram emits *_bucket / *_sum / *_count lines
    assert "msai_registry_bootstrap_duration_ms_bucket" in rendered
    assert "msai_registry_bootstrap_duration_ms_count" in rendered
