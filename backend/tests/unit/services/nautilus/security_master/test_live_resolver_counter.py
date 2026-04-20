"""Unit test: confirm the live-resolver counter is registered in the
project's hand-rolled MetricsRegistry and exposes the .labels(...).inc()
interface that `lookup_for_live` uses.

Registry introspection only — do NOT .inc() the shared process-global
counter in this test; that would pollute the value seen by other tests
that snapshot counter state.
"""

from __future__ import annotations

from msai.services.observability import get_registry
from msai.services.observability.metrics import Counter
from msai.services.observability.trading_metrics import (
    LIVE_INSTRUMENT_RESOLVED_TOTAL,
)


def test_counter_is_registered_in_metrics_registry() -> None:
    registry = get_registry()
    assert "msai_live_instrument_resolved_total" in registry._metrics


def test_counter_is_a_counter_not_a_gauge() -> None:
    registry = get_registry()
    metric = registry._metrics["msai_live_instrument_resolved_total"]
    assert isinstance(metric, Counter), f"expected Counter, got {type(metric).__name__}"


def test_module_level_reference_is_same_instance_as_registry() -> None:
    """`_r.counter(...)` is idempotent — the module-level reference
    must equal the registry's stored instance."""
    registry = get_registry()
    assert (
        LIVE_INSTRUMENT_RESOLVED_TOTAL is registry._metrics["msai_live_instrument_resolved_total"]
    )


def test_counter_supports_labels_interface() -> None:
    """`.labels(source=..., asset_class=...).inc()` is the call shape
    `lookup_for_live` uses. Verify the Counter exposes a `.labels()`
    method that returns an incrementable bound view.
    """
    labeled = LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
        source="registry",
        asset_class="equity",
    )
    assert hasattr(labeled, "inc")
