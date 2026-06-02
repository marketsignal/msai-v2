"""Pre-registered Prometheus metrics for the broker-account fleet (Task 7).

Import and call ``.inc()`` / ``.set()`` at the relevant lifecycle points.
The metrics are created lazily on first import — no side effects at
module-load time beyond the registry lookup. Mirrors the module-level
pattern in ``trading_metrics.py``.

Usage::

    from msai.services.observability.broker_account_metrics import (
        KV_SECRET_AGE,
        SPAWN_FAILED,
    )

    SPAWN_FAILED.inc(account_id="DU1", reason="kv_unauthorized")
    KV_SECRET_AGE.set(123.0, account_id="DU1")
"""

from __future__ import annotations

from msai.services.observability import get_registry

_r = get_registry()

# Broker-account spawn failures, labeled by account and KV failure reason at
# increment time via ``.inc(account_id=..., reason=...)`` per the project's
# hand-rolled Counter API (metrics.py:117).
SPAWN_FAILED = _r.counter(
    "msai_broker_account_spawn_failed_total",
    "Broker account spawn failures by account and KV failure reason",
)

# Age in seconds of a broker account's stored credential secret. Labeled by
# account at set time via ``.set(value, account_id=...)``. Drives rotation
# enforcement / alerting on stale credentials.
KV_SECRET_AGE = _r.gauge(
    "msai_kv_secret_age_seconds",
    "Age in seconds of a broker account's stored credential secret (rotation enforcement)",
)
