"""Observability primitives for MSAI live trading.

Currently exposes a thin Prometheus metrics registry built
without the ``prometheus_client`` library — the exposition
format is plain text and we only need a handful of counters /
gauges, so a 200-line in-process registry keeps the dependency
surface small. If we ever need histograms with adaptive
buckets or cardinality-aware label storage, swap this for the
upstream library.
"""

from msai.services.observability.metrics import (
    Counter,
    Gauge,
    MetricsRegistry,
    get_registry,
)

__all__ = [
    "Counter",
    "Gauge",
    "MetricsRegistry",
    "get_registry",
]
