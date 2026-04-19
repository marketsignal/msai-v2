"""In-process Prometheus metrics registry (Phase 4 task 4.6).

A minimal, dependency-free implementation of the Prometheus
text exposition format. We only need counters and gauges with
optional labels — no histograms or summaries — so a hand-rolled
registry stays simpler than pulling in ``prometheus_client``.

Why hand-rolled instead of ``prometheus_client``:

- Avoids a new third-party dependency
- The exposition format is fully documented and stable since
  Prometheus 2.0
- We control the label cardinality, so a label-aware in-memory
  dict is enough — no atomic-counter wrappers needed because
  Python's GIL serializes ``+= 1`` on a dict value
- Easy to unit-test without standing up a registry singleton

Public API:

.. code-block:: python

    from msai.services.observability import get_registry

    registry = get_registry()
    counter = registry.counter(
        "msai_orders_submitted_total",
        "Total live orders submitted to the broker.",
    )
    counter.inc()
    counter.labels(deployment_id="abc").inc()

    gauge = registry.gauge(
        "msai_active_deployments",
        "Number of deployments currently in 'running' status.",
    )
    gauge.set(3)

    print(registry.render())  # Prometheus text format

The FastAPI ``/metrics`` endpoint calls :meth:`MetricsRegistry.render`
on every scrape. The endpoint is not authenticated — operators
expose it on a private network or behind a reverse proxy.

Thread safety: the GIL makes single-counter ``+= 1`` atomic, but
the labels dict mutation is NOT atomic across threads. We use a
single ``threading.Lock`` per registry to serialize label
creation. The hot path (existing-label increment) is then
lock-free (dict access under the GIL).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def _format_labels(labels: Mapping[str, str]) -> str:
    """Render labels as ``{k="v",k2="v2"}`` per the Prometheus
    text format. Empty labels render as an empty string."""
    if not labels:
        return ""
    parts = [f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _escape(value: str) -> str:
    """Escape backslash, double-quote, and newline per the
    Prometheus text format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _LabeledMetric:
    """Common base for ``Counter`` and ``Gauge``. Owns the
    label-keyed value dict and the metric-level lock."""

    metric_type: str = "untyped"

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(labels.items()))

    def render(self) -> list[str]:
        """Render this metric in Prometheus text format. Each
        metric block produces a HELP line, a TYPE line, and one
        sample line per label combination."""
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} {self.metric_type}",
        ]
        with self._lock:
            if not self._values:
                lines.append(f"{self.name} 0")
            else:
                for label_key, value in sorted(self._values.items()):
                    labels = dict(label_key)
                    rendered = _format_labels(labels)
                    lines.append(f"{self.name}{rendered} {value}")
        return lines


class Counter(_LabeledMetric):
    """Monotonic counter — only goes up. Decrements raise to
    catch programmer errors at write time rather than
    discovering them via a confusing graph."""

    metric_type = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"counter {self.name} cannot be decremented")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def labels(self, **labels: str) -> _CounterChild:
        return _CounterChild(self, labels)


class _CounterChild:
    """Bound view of a labeled counter — lets the call site
    cache the label set so the increment path is one
    dict-add, mirroring ``prometheus_client``'s API."""

    def __init__(self, parent: Counter, labels: Mapping[str, str]) -> None:
        self._parent = parent
        self._labels = dict(labels)

    def inc(self, amount: float = 1.0) -> None:
        self._parent.inc(amount, **self._labels)


class Gauge(_LabeledMetric):
    """Arbitrary point-in-time value. Can go up or down."""

    metric_type = "gauge"

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def labels(self, **labels: str) -> _GaugeChild:
        return _GaugeChild(self, labels)


class _GaugeChild:
    def __init__(self, parent: Gauge, labels: Mapping[str, str]) -> None:
        self._parent = parent
        self._labels = dict(labels)

    def set(self, value: float) -> None:
        self._parent.set(value, **self._labels)

    def inc(self, amount: float = 1.0) -> None:
        self._parent.inc(amount, **self._labels)

    def dec(self, amount: float = 1.0) -> None:
        self._parent.dec(amount, **self._labels)


class MetricsRegistry:
    """Per-process registry of all metrics. Counters and
    gauges live here; the FastAPI ``/metrics`` endpoint
    iterates them and emits the exposition text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _LabeledMetric] = {}

    def counter(self, name: str, help_text: str) -> Counter:
        """Get-or-create a counter. Idempotent — calling
        twice with the same name returns the same instance,
        so module-level callers don't have to coordinate
        registration."""
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Counter):
                    raise TypeError(
                        f"metric {name!r} already registered as {type(existing).__name__}"
                    )
                return existing
            counter = Counter(name, help_text)
            self._metrics[name] = counter
            return counter

    def gauge(self, name: str, help_text: str) -> Gauge:
        with self._lock:
            existing = self._metrics.get(name)
            if existing is not None:
                if not isinstance(existing, Gauge):
                    raise TypeError(
                        f"metric {name!r} already registered as {type(existing).__name__}"
                    )
                return existing
            gauge = Gauge(name, help_text)
            self._metrics[name] = gauge
            return gauge

    def render(self) -> str:
        """Render every registered metric in Prometheus text
        format. Each metric block ends with a newline so the
        full body is parseable by Prometheus's scraper."""
        with self._lock:
            metrics = list(self._metrics.values())
        sections: list[str] = []
        for metric in sorted(metrics, key=lambda m: m.name):
            sections.append("\n".join(metric.render()))
        return "\n".join(sections) + "\n"

    def reset(self) -> None:
        """Drop every registered metric. Used by tests so a
        per-test registry isn't polluted by the previous
        test's writes."""
        with self._lock:
            self._metrics.clear()


_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """Per-process singleton registry. The first call builds
    the instance lazily; subsequent calls return the same
    object."""
    global _registry  # noqa: PLW0603 — lazy singleton
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry
