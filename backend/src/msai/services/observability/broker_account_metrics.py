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

# Deploy-validation rejections at ``/live/start-portfolio`` (Task 5). The
# cheap row-state stage (``validate_account_row_state``) is fail-closed: a
# resolved broker account that is archived / mode-inconsistent / not routable /
# not bound to its login blocks the deploy BEFORE any node spawns. Labeled by
# account + reason at increment time via ``.inc(account_id=..., reason=...)``.
#
# STAGE-2 credential-validation rejections are split by reason:
#   * ``archived`` (the defensive archived-between-stages branch in
#     ``validate_account_credentials``) IS counted here — it is not counted
#     anywhere else, so this is the only alert signal for it.
#   * ``CredentialResolutionError`` (KV unreachable / unauthorized / missing) is
#     NOT counted here — it is already counted inside ``resolve_for_spawn``
#     (``SPAWN_FAILED``), so a single failure is never double-counted.
# (There is intentionally no ``login_mismatch`` reason: ``ib_login_key`` is a
# routing alias, not the TWS username, so a userid↔login check was removed.)
DEPLOY_VALIDATION_FAILED = _r.counter(
    "msai_broker_account_deploy_validation_failed_total",
    "Broker account deploy-validation rejections by account and row-state reason",
)

# PR4: NON-GATING divergence audit (council objection #7). Incremented when the
# UI told the handler which account its global selector was scoped to
# (``selector_context_account_id``) AND that concrete account differs from the
# resolved deploy target. The bucket sentinels ("all"/"unassigned") are NOT a
# focused target and are excluded by the caller. Labeled by the resolved deploy
# target account at increment time via ``.inc(account_id=...)``. Never blocks a
# deploy — it is a server-side audit signal only.
DEPLOY_TARGET_DIVERGENCE = _r.counter(
    "msai_broker_account_deploy_target_divergence_total",
    "Deploys where the UI global-selector context differed from the resolved target account",
)
