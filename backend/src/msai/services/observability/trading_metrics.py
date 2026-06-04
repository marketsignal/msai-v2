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
DEPLOYMENTS_STARTED = _r.counter("msai_deployments_started_total", "Live deployments started")
DEPLOYMENTS_STOPPED = _r.counter("msai_deployments_stopped_total", "Live deployments stopped")
DEPLOYMENTS_FAILED = _r.counter("msai_deployments_failed_total", "Live deployments failed")

# Kill switch
KILL_SWITCH_ACTIVATED = _r.counter("msai_kill_switch_total", "Kill switch activations")

# Stop-flatness verification — Redis-coordinated child→API report of
# deployment-scoped position state at shutdown. See
# `docs/decisions/redis-flatness-protocol.md`.
FLATNESS_REQUESTS_TOTAL = _r.counter(
    "msai_flatness_requests_total",
    "Total /stop or /kill-all calls that issued a STOP_AND_REPORT_FLATNESS "
    "command. Increments on every API invocation regardless of coalescing "
    "outcome. Pair with msai_flatness_coalesced_total for hit rate.",
)
FLATNESS_COALESCED_TOTAL = _r.counter(
    "msai_flatness_coalesced_total",
    "Count of API stop calls that coalesced onto an existing in-flight "
    "stop_nonce (SET-NX inflight_stop:{deployment_id} returned False). "
    "Hit rate = msai_flatness_coalesced_total / msai_flatness_requests_total.",
)
FLATNESS_POLL_TIMEOUT_TOTAL = _r.counter(
    "msai_flatness_poll_timeout_total",
    "Count of API polls that hit the 30s (/stop) or 15s (/kill-all) "
    "deadline without ever observing the child's stop_report:{nonce} key. "
    "Investigate child shutdown or Redis health when this trends > 0.",
)
FLATNESS_REPORT_NON_FLAT_TOTAL = _r.counter(
    "msai_flatness_report_non_flat_total",
    "Count of stop_reports returning broker_flat=False — Nautilus market_exit "
    "exhausted max_attempts while positions remained. Each increment requires "
    "operator IB-portal review per the runbook.",
)
FLATNESS_PENDING_LIST_LENGTH = _r.gauge(
    "msai_flatness_pending_list_length",
    "RPUSH list length on flatness_pending:{deployment_id} just before SIGTERM. "
    "Healthy: 1. Sustained > 1 means concurrent stops are stacking on a "
    "stuck child (coalescing should normally hold this at 1).",
)

# Order lifecycle
ORDERS_SUBMITTED = _r.counter("msai_orders_submitted_total", "Orders submitted to broker")
ORDERS_FILLED = _r.counter("msai_orders_filled_total", "Orders filled by broker")
ORDERS_DENIED = _r.counter("msai_orders_denied_total", "Orders denied by risk checks")

# IB connectivity
IB_DISCONNECTS = _r.counter("msai_ib_disconnects_total", "IB Gateway disconnect events")

# Data freshness (PR 1b) — fleet halts fired by the in-node data-stale monitor
# when a required Databento feed/dataset goes stale past its grace budget. One
# increment per stale TRANSITION (warm→stale), not per tick — the monitor is
# idempotent while a feed remains stale.
#
# This is a Redis-hydrated GAUGE (not a counter): the monitor's local registry
# inc happens in the live SUBPROCESS, invisible to the API /metrics registry,
# so the account-labeled series here is REPLACED each scrape from the
# ``msai:metrics:data_stale_halts:{account_id}`` Redis counter
# (hydrate_data_health_metrics, same SCAN pattern as IB_EXEC_PACING_ERRORS).
DATA_STALE_HALTS = _r.gauge(
    "msai_data_stale_halts_total",
    "Fleet halts fired by the in-node data-stale monitor (warm→stale "
    "transition), per account. Redis-hydrated from the "
    "msai:metrics:data_stale_halts:{account_id} counter INCRed by the monitor.",
)

# Data-feed health (PR 1b T7) — Redis-hydrated, labeled gauges. ALL series are
# populated by ``hydrate_data_health_metrics`` (manifest-first reader, same as
# the GET /api/v1/live/data-health route) and REPLACED each hydrate via
# ``Gauge.replace_children`` so a feed that drops out of the monitor's manifest
# disappears from the next scrape rather than lingering forever.
DATA_FEED_AGE_SECONDS = _r.gauge(
    "msai_data_feed_age_seconds",
    "Seconds since the last observed event for a required Databento feed, "
    "labeled by account/node/dataset/symbol/feed. Redis-hydrated from the "
    "in-node data-stale monitor's per-feed freshness rows.",
)
DATA_FEED_STALE = _r.gauge(
    "msai_data_feed_stale",
    "1 if a required Databento feed is not warm (stale, pending, or missing), "
    "else 0, labeled by account/node/dataset/symbol/feed. Redis-hydrated "
    "alongside msai_data_feed_age_seconds.",
)
DATABENTO_DATASET_ALIVE = _r.gauge(
    "msai_databento_dataset_alive",
    "1 if every required feed in a Databento dataset is warm, else 0 — "
    "dataset-granularity connection health derived from the per-feed verdicts, "
    "labeled by account/node/dataset.",
)
IB_EXEC_PACING_ERRORS = _r.gauge(
    "msai_ib_exec_pacing_errors",
    "Count of IB EXEC-side pacing/throttle order rejections per account, "
    "Redis-hydrated from the msai:metrics:ib_exec_pacing:{account_id} counter "
    "INCRed by the live node's OrderRejected audit branch.",
)
# FIX 4 — an ACTIVE deployment whose freshness manifest is ABSENT or MALFORMED
# means its in-node data-stale monitor never started / died (and the manifest
# TTL lapsed). That is distinct from an empty fleet: the deployment IS running
# but its data-stale protection is dark. Hydrated (and pruned via
# replace_children) on the same manifest-first reader path so Prometheus can
# alert on a dead monitor — 1 per monitor-missing deployment, labeled by
# deployment.
DATA_MONITOR_MISSING = _r.gauge(
    "msai_data_monitor_missing",
    "1 if an active deployment's in-node data-stale monitor is missing/dead "
    "(freshness manifest absent or malformed), labeled by deployment. "
    "Redis-hydrated alongside the data-feed health series.",
)

# Active deployments gauge
ACTIVE_DEPLOYMENTS = _r.gauge("msai_active_deployments", "Currently active deployments")

# Live-start instrument resolution outcomes
LIVE_INSTRUMENT_RESOLVED_TOTAL = _r.counter(
    "msai_live_instrument_resolved_total",
    "Count of instrument resolutions on the live-start critical path.",
)
# Labels applied at increment time via
# ``.labels(source=..., asset_class=...).inc()`` per the project's hand-rolled
# Counter API (metrics.py:116-138). ``source`` ∈ {registry, registry_miss,
# registry_incomplete}; ``asset_class`` mirrors ``AssetClass`` values plus
# ``unknown`` when the row is unresolvable.

# Coverage scan outcomes — emitted by compute_coverage when missing_ranges
# is non-empty (status="gapped" exit). Labels:
#   asset_class — ingest-side asset class (matches AssetClass enum)
#   symbol — the registered symbol with the gap
# Use for gap-rate dashboards + alert rules. Production-vs-staging cohort
# filtering is delegated to alert rules (no is_production flag in the registry
# yet — see Task 9 + plan Implementation Notes "scoped deviation" on Hawk #5).
COVERAGE_GAP_DETECTED = _r.counter(
    "msai_coverage_gap_detected_total",
    "Number of compute_coverage calls that returned non-empty missing_ranges, "
    "labeled by symbol/asset_class. Use for gap-rate dashboards and alert rules.",
)

# Backtest results payload size — observed at worker-write (post-JSONB
# materialization) AND /results response. Detects accidental payload bloat
# (e.g. minute-bar leak into the JSONB) on either hop.
_1_KB = 1_024
_10_KB = 10_240
_100_KB = 102_400
_1_MB = 1_048_576
_10_MB = 10_485_760
msai_backtest_results_payload_bytes = _r.histogram(
    "msai_backtest_results_payload_bytes",
    "Size in bytes of the Backtest.series JSONB payload "
    "(observed at worker-write + /results response).",
    buckets=(_1_KB, _10_KB, _100_KB, _1_MB, _10_MB),
)

# Paginated /trades endpoint — labeled by page_size bucket.
msai_backtest_trades_page_count = _r.counter(
    "msai_backtest_trades_page_count",
    "Count of GET /api/v1/backtests/{id}/trades requests, labeled by page_size.",
)

# --- Databento registry bootstrap observability ---

# Databento API call outcomes
DATABENTO_API_CALLS_TOTAL = _r.counter(
    "msai_databento_api_calls_total",
    "Databento API calls partitioned by endpoint and outcome. "
    "Outcomes: success, rate_limited_recovered, rate_limited_failed, "
    "unauthorized, upstream_error.",
)

# Registry bootstrap outcomes (per-symbol)
REGISTRY_BOOTSTRAP_TOTAL = _r.counter(
    "msai_registry_bootstrap_total",
    "Registry bootstrap outcomes partitioned by provider, asset_class, outcome.",
)

# Bootstrap latency histogram (1 symbol end-to-end), milliseconds.
_BOOTSTRAP_BUCKETS_MS: tuple[int, ...] = (100, 500, 1_000, 2_000, 5_000, 10_000, 30_000)
REGISTRY_BOOTSTRAP_DURATION_MS = _r.histogram(
    "msai_registry_bootstrap_duration_ms",
    "End-to-end latency per bootstrap operation (1 symbol), in milliseconds.",
    buckets=_BOOTSTRAP_BUCKETS_MS,
)

# Divergence counter. Fires when IB refresh writes an alias whose venue
# differs from a prior Databento-authored alias for the same instrument
# definition. Real-migration-only semantics enforced by alias normalization
# (notation-only diffs like XNAS vs NASDAQ do NOT fire).
REGISTRY_VENUE_DIVERGENCE_TOTAL = _r.counter(
    "msai_registry_venue_divergence_total",
    "Fires when IB refresh writes an alias whose venue differs from a prior "
    "Databento-authored alias for the same instrument definition. "
    "Labels applied at increment time: databento_venue, ib_venue.",
)

# --- Symbol onboarding observability ---

# Run-level outcome counter. Labeled by terminal status at increment time.
onboarding_jobs_total = _r.counter(
    "msai_onboarding_jobs_total",
    "Symbol-onboarding runs by terminal status. "
    "Labels applied at increment time: status (completed | "
    "completed_with_failures | failed).",
)

# Per-symbol per-phase duration in seconds. Unlabeled — the project's
# hand-rolled Histogram primitive (services/observability/metrics.py)
# does not yet support labeled observations. Per-step breakdown lives
# in the structured ``symbol_onboarding_step_completed`` log event.
_ONBOARDING_BUCKETS_S: tuple[int, ...] = (1, 5, 15, 30, 60, 120, 300, 600)
onboarding_symbol_duration_seconds = _r.histogram(
    "msai_onboarding_symbol_duration_seconds",
    "Per-symbol end-to-end onboarding duration in seconds.",
    buckets=_ONBOARDING_BUCKETS_S,
)

# IB-qualification timeout counter (council-mandated SLA guardrail).
onboarding_ib_timeout_total = _r.counter(
    "msai_onboarding_ib_timeout_total",
    "Count of IB qualification phases that exceeded the configured timeout.",
)
