"""Pre-registered Prometheus counters for the trading lifecycle.

Import and call ``.inc()`` at each lifecycle point. The counters
are created lazily on first import — no side effects at
module-load time beyond the registry lookup.

Usage::

    from msai.services.observability.trading_metrics import DEPLOYMENTS_STARTED
    DEPLOYMENTS_STARTED.inc()
    DEPLOYMENTS_STARTED.inc(strategy="ema_cross")  # with label
"""

from __future__ import annotations

from msai.services.observability import get_registry

_r = get_registry()

# Live deployment lifecycle
DEPLOYMENTS_STARTED = _r.counter(
    "msai_deployments_started_total", "Live deployments started"
)
DEPLOYMENTS_STOPPED = _r.counter(
    "msai_deployments_stopped_total", "Live deployments stopped"
)
DEPLOYMENTS_FAILED = _r.counter(
    "msai_deployments_failed_total", "Live deployments failed"
)

# Kill switch
KILL_SWITCH_ACTIVATED = _r.counter(
    "msai_kill_switch_total", "Kill switch activations"
)

# Order lifecycle
ORDERS_SUBMITTED = _r.counter(
    "msai_orders_submitted_total", "Orders submitted to broker"
)
ORDERS_FILLED = _r.counter(
    "msai_orders_filled_total", "Orders filled by broker"
)
ORDERS_DENIED = _r.counter(
    "msai_orders_denied_total", "Orders denied by risk checks"
)

# IB connectivity
IB_DISCONNECTS = _r.counter(
    "msai_ib_disconnects_total", "IB Gateway disconnect events"
)

# Active deployments gauge
ACTIVE_DEPLOYMENTS = _r.gauge(
    "msai_active_deployments", "Currently active deployments"
)
