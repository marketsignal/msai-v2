"""Redis halt-key namespacing for the live-trading fleet.

Single source of truth — replaces three duplicate ``_HALT_KEY = "msai:risk:halt"``
definitions across api/live.py, live_supervisor/fleet_router.py, and
services/nautilus/disconnect_handler.py.

Halt-cause metadata is written as a companion key alongside the latch key so
operators can distinguish a manual ``/kill-all`` panic from a PR-1b data-stale
auto-halt. Per the decision-doc addendum 2026-05-28, reuse the same latch code
path; only the cause-attribution metadata differs.
"""

from __future__ import annotations

from enum import StrEnum

_FLEET_HALT = "msai:risk:halt"

# ---------------------------------------------------------------------------
# Shared halt-write retry/TTL constants
# ---------------------------------------------------------------------------

HALT_TTL_SECONDS = 86400
"""24h TTL on the fleet halt latch + companions. Same window the API's
``/kill-all`` uses, so a data-stale or IB-disconnect auto-halt has the same
operator recovery behaviour as a manual kill switch — a forgotten halt
self-expires after 24h rather than permanently bricking the platform."""

HALT_SET_MAX_ATTEMPTS = 5
"""Retries of the atomic Lua halt write before giving up. With exponential
backoff from 100ms the total wait is ~3.1s — long enough to ride out a
transient Redis blip without leaving the platform fail-OPEN."""

HALT_SET_BACKOFF_S = 0.1
"""Initial backoff between halt-write retry attempts; doubles each attempt
(100ms, 200ms, 400ms, 800ms, 1.6s)."""


class HaltCause(StrEnum):
    """Distinguishes halt callers so an operator can interpret a halt latch."""

    FLEET_EMERGENCY = "fleet_emergency"  # Manual /kill-all
    OPERATOR_DRAIN = "operator_drain"  # Account-scoped drain
    DATA_STALE = "data_stale"  # PR 1b — Databento freshness gate
    IB_DISCONNECT = "ib_disconnect"  # PR 1b — extended IB Gateway outage


def fleet_halt_key() -> str:
    """Return the global fleet-wide halt latch key."""
    return _FLEET_HALT


def account_halt_key(account_id: str) -> str:
    """Return the account-scoped halt latch key for *account_id*.

    PR 1 critical: keyed by ``account_id`` (DUP733214, DUP733215, …),
    NOT by ``ib_login_key``, so that draining one sub-account under a
    shared login does NOT halt the other sub-account.

    Citation: council 2026-05-27 blocking objection #4 — "Split halt
    semantics: account-scoped halt/drain + explicit fleet emergency
    halt". (F7 fix: the prior citation pointed at council 2026-05-29
    objection #11, which is actually about IB Gateway client-ID
    isolation — see ``docs/decisions/multi-account-broker-fleet.md:203``.)
    """
    if not account_id:
        raise ValueError("account_id must be non-empty")
    return f"{_FLEET_HALT}:account:{account_id}"


def halt_cause_key(scope: str, *, account_id: str | None = None) -> str:
    """Return the companion key holding ``HaltCause`` metadata for *scope*."""
    if scope == "fleet":
        return f"{_FLEET_HALT}:cause"
    if scope == "account":
        if not account_id:
            raise ValueError("account_id required when scope='account'")
        return f"{_FLEET_HALT}:account:{account_id}:cause"
    raise ValueError(f"unknown halt scope: {scope!r}")


# ---------------------------------------------------------------------------
# PR 1b — data-freshness Redis namespacing
# ---------------------------------------------------------------------------


def data_freshness_manifest_key(deployment_id: str) -> str:
    """Return the key holding a deployment's REQUIRED-FEED manifest.

    The data-stale monitor publishes, at start and on every tick, the SET of
    :class:`~msai.services.live.data_freshness.FeedKey` that the deployment is
    *required* to keep warm — serialized as a JSON list of
    ``{dataset, feed, symbol}`` objects (``feed`` = the native bar-type string).

    Manifest semantics the API depends on (PR 1b):

    * **Absent** key  → the monitor never started (or its node is gone and the
      manifest TTL — 3× the monitor tick interval — has lapsed).
    * **Empty list**  → the monitor is running but this node requires NO
      Databento feeds (legacy / non-Databento path); distinct from absent.
    * **Non-empty**   → the listed feeds are the freshness universe; the API
      can derive a ``missing`` verdict for any manifest feed lacking a live
      per-feed freshness key.
    """
    if not deployment_id:
        raise ValueError("deployment_id must be non-empty")
    return f"msai:data:freshness:{deployment_id}:manifest"


def data_freshness_key(deployment_id: str, dataset: str, native_bar_type: str) -> str:
    """Return the per-feed freshness key for one ``(dataset, bar-type)`` feed.

    ``native_bar_type`` is the FULL native Nautilus BarType string (e.g.
    ``AAPL.XNAS-1-MINUTE-LAST-EXTERNAL``) so two intervals on the same symbol
    map to DISTINCT keys. The monitor writes the per-feed freshness JSON to
    this key and a plain-string ``warm``/``pending``/``stale`` verdict to this
    key suffixed with ``:verdict`` (see :data:`VERDICT_KEY_SUFFIX`), both with a
    TTL of 3× the monitor tick interval.
    """
    if not deployment_id:
        raise ValueError("deployment_id must be non-empty")
    if not dataset:
        raise ValueError("dataset must be non-empty")
    if not native_bar_type:
        raise ValueError("native_bar_type must be non-empty")
    return f"msai:data:freshness:{deployment_id}:{dataset}:{native_bar_type}"


VERDICT_KEY_SUFFIX = ":verdict"
"""Suffix appended to a per-feed :func:`data_freshness_key` for the companion
plain-string verdict key (``warm`` | ``pending`` | ``stale``). The monitor
publishes ``warm`` (fresh data observed), ``pending`` (no observation yet,
within startup grace — NOT resumable), or ``stale``. ``missing`` is NOT
published — it is API-derived (manifest-minus-live-keys) and stays distinct
from ``pending`` (monitor alive, feed unobserved). Only ``warm`` is resumable;
``RESUME_CLEAR_LUA``'s bare ``GET == 'warm'`` therefore blocks ``pending`` and
``stale`` with no Lua change."""


def ib_exec_pacing_key(account_id: str) -> str:
    """Return the plain Redis counter key for IB EXEC-side pacing/throttle
    rejections on *account_id* (PR 1b T7).

    INCRed by the live node's engine-level OrderRejected audit branch whenever a
    rejection reason matches an exec-side throttle phrase (``pacing violation`` /
    ``max rate of messages`` / ``throttle`` — NOT the legacy market-data pacing
    codes 100/162/420). The data-health API hydrates this counter into the
    ``msai_ib_exec_pacing_errors`` gauge keyed by ``account``.
    """
    if not account_id:
        raise ValueError("account_id must be non-empty")
    return f"msai:metrics:ib_exec_pacing:{account_id}"


def data_stale_halts_key(account_id: str) -> str:
    """Return the plain Redis counter key for data-stale auto-halts on
    *account_id* (PR 1b).

    INCRed by the in-node data-stale monitor on each warm→stale transition
    where the fleet halt fires (one per transition, idempotent while stale).
    The monitor runs in the live SUBPROCESS, whose in-process Prometheus
    registry is invisible to the API ``/metrics`` render path — so this Redis
    counter is the source of truth. ``hydrate_data_health_metrics`` SCANs these
    keys into the ``msai_data_stale_halts_total`` series keyed by ``account``,
    exactly like :func:`ib_exec_pacing_key`.
    """
    if not account_id:
        raise ValueError("account_id must be non-empty")
    return f"msai:metrics:data_stale_halts:{account_id}"


def reconciled_key(deployment_id: str) -> str:
    """Return the key marking a deployment's node as having completed a healthy
    Nautilus reconciliation (the "node is genuinely running" marker, PR 1b T4).

    Lifecycle (owned by the live subprocess run loop):

    * **DELETEd** at subprocess start, BEFORE ``node`` is built/started, so a
      restart RE-ARMS the fail-closed default — the marker is never carried over
      from a prior incarnation of the same deployment.
    * **SET** (to an ISO-8601 wall-clock string, no TTL) immediately AFTER the
      node reaches its ``running`` row state — i.e. after ``_mark_running``
      succeeds, which is itself gated by ``wait_until_ready`` polling
      ``trader.is_running``.

    Why ``trader.is_running`` is a sound proxy for "reconciliation completed":
    in the installed ``nautilus_trader 1.223.0``, ``kernel.py:1025-1037`` returns
    EARLY from ``start_async`` on a failed / timed-out reconciliation, so
    ``trader.start()`` is never reached and ``trader.is_running`` stays ``False``.
    ``wait_until_ready`` therefore cannot pass without a healthy reconcile. Do
    NOT read ``ExecutionEngine.reconciliation`` to decide this — that property is
    the config FLAG (whether reconciliation is enabled), not a completion signal
    (``execution_engine.py:260``).

    The API / monitor read this marker as the "is this node trustworthy?" signal;
    an absent marker means "not yet reconciled" (fail-closed).
    """
    if not deployment_id:
        raise ValueError("deployment_id must be non-empty")
    return f"msai:live:reconciled:{deployment_id}"


# ---------------------------------------------------------------------------
# Atomic halt-write Lua script
# ---------------------------------------------------------------------------

HALT_WRITE_LUA = """
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[5])
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[5])
redis.call('set', KEYS[3], ARGV[3], 'EX', ARGV[5])
redis.call('set', KEYS[4], ARGV[4], 'NX', 'EX', ARGV[5])
redis.call('expire', KEYS[4], ARGV[5])
redis.call('lpush', KEYS[5], ARGV[4])
redis.call('ltrim', KEYS[5], 0, 49)
return 1
"""
"""Atomic fleet-halt write — ONE round-trip, all-or-nothing (preserves the
F9 all-or-nothing guarantee).

``KEYS`` (in order):

1. ``latch``    — fleet halt latch (:func:`fleet_halt_key`)
2. ``set_by``   — latch ``:set_by`` companion
3. ``set_at``   — latch ``:set_at`` companion
4. ``cause``    — :func:`halt_cause_key('fleet') <halt_cause_key>`
5. ``history``  — ``halt_cause_key('fleet') + ':history'`` LIST

``ARGV`` (in order):

1. ``latch_val``   — latch value (``'true'``)
2. ``set_by_val``  — who set the latch (e.g. ``data_stale_monitor:<node_id>``)
3. ``set_at_val``  — ISO-8601 wall-clock of the halt
4. ``cause_json``  — the canonical cause JSON (see below); written to the cause
   key ONLY-IF-ABSENT (``SET ... NX``) so a pre-existing manual ``/kill-all``
   cause is PRESERVED, and ALWAYS ``LPUSH``-ed onto the capped history list
5. ``ttl_seconds`` — TTL for latch/set_by/set_at/cause (24h for an auto-halt)

The script: SET latch+EX, SET set_by+EX, SET set_at+EX, SET cause NX EX
(preserve existing cause — a plain SETNX inside MULTI/EXEC can't read-then-decide,
which is why this is a script), then UNCONDITIONALLY EXPIRE the cause key to the
same TTL, LPUSH history cause_json, LTRIM history 0 49, return 1.

The unconditional ``EXPIRE`` after the ``SET ... NX`` is the fix for a stale-
attribution bug: ``SET NX`` preserves a PRE-EXISTING cause's value but leaves its
OLD (possibly shorter) TTL untouched, while the latch/companions are refreshed to
the full 24h window. Without the explicit ``EXPIRE``, a long-running halt could
keep its latch alive while the preserved cause key expired early — leaving an
active halt with NO attribution. The ``EXPIRE`` refreshes the preserved cause's
TTL without overwriting its value, and is a no-op-safe refresh when the ``SET NX``
just created the key (the EX already set it; EXPIRE re-sets to the same window).

**Canonical cause JSON shape** (the value of ARGV[4]) is a REASON-TAGGED UNION:
every caller stamps a ``reason`` (= :class:`HaltCause` value) plus a common
``deployment_id`` + ``detected_at``, and the remaining fields vary by reason.
Keep in sync with the API reader. The two auto-halt variants:

``reason == "data_stale"`` (the in-node data-stale monitor)::

    {
      "reason": "data_stale",          # HaltCause.DATA_STALE.value
      "account_id": "<account_id>",
      "node_id": "<node_id>",
      "deployment_id": "<deployment_id>",
      "dataset": "<databento dataset>",
      "feed": "<native bar-type str | null>",   # null for a dataset-granularity finding
      "symbol": "<symbol | null>",              # null for a dataset-granularity finding
      "detected_at": "<ISO-8601 utc>",
      "last_event_ts": <epoch-ns int | null>    # null for an absent (never-observed) feed
    }

``reason == "ib_disconnect"`` (the IB disconnect handler) — carries a
``source`` field and OMITS the data-stale-only ``dataset`` / ``feed`` /
``symbol`` / ``last_event_ts`` fields (the handler watches one node's IB
connection, not a per-feed Databento stream)::

    {
      "reason": "ib_disconnect",       # HaltCause.IB_DISCONNECT.value
      "account_id": null,              # handler knows only the deployment slug
      "node_id": null,
      "deployment_id": "<deployment_slug>",
      "detected_at": "<ISO-8601 utc>",
      "source": "ib_disconnect_handler:<deployment_slug>"
    }

(The manual ``/kill-all`` cause is a third, smaller variant —
``reason == "fleet_emergency"`` with ``detected_at`` + ``source``.)
"""


def fleet_halt_write_args(
    *,
    set_by: str,
    set_at: str,
    cause_json: str,
    ttl_s: int,
) -> tuple[list[str], list[str]]:
    """Build the ``(keys, argv)`` for an atomic fleet-halt write via
    :data:`HALT_WRITE_LUA`.

    Owns the 5-key ordering — ``[latch, :set_by, :set_at, cause, :history]`` —
    and the matching ARGV ordering so the positional coupling between the Lua
    script and its callers lives in ONE place. Callers pass only the values;
    they no longer hand-assemble the key/argv lists (which previously diverged
    in subtle ways across the three halt-write sites).

    Returns ``(keys, argv)``; the caller invokes
    ``redis.eval(HALT_WRITE_LUA, len(keys), *keys, *argv)``.
    """
    fleet = fleet_halt_key()
    cause = halt_cause_key("fleet")
    keys = [fleet, f"{fleet}:set_by", f"{fleet}:set_at", cause, f"{cause}:history"]
    argv = ["true", set_by, set_at, cause_json, str(ttl_s)]
    return keys, argv


# ---------------------------------------------------------------------------
# Atomic resume check-and-clear Lua script (PR 1b T6)
# ---------------------------------------------------------------------------

RESUME_CLEAR_LUA = """
local n_manifest = tonumber(ARGV[1])
local n_verdict = tonumber(ARGV[2])
local n_reconciled = tonumber(ARGV[3])
local idx = 0
for i = 1, n_manifest do
  idx = idx + 1
  if redis.call('exists', KEYS[idx]) == 0 then
    return 'MANIFEST_MISSING:' .. KEYS[idx]
  end
end
for i = 1, n_verdict do
  idx = idx + 1
  local v = redis.call('get', KEYS[idx])
  if v == false then
    return 'VERDICT_MISSING:' .. KEYS[idx]
  end
  if v ~= 'warm' then
    return 'VERDICT_NOT_WARM:' .. KEYS[idx]
  end
end
for i = 1, n_reconciled do
  idx = idx + 1
  if redis.call('exists', KEYS[idx]) == 0 then
    return 'RECONCILED_MISSING:' .. KEYS[idx]
  end
end
for i = idx + 1, #KEYS do
  redis.call('del', KEYS[i])
end
return 'OK'
"""
"""Atomic resume precondition re-verify + clear — ONE round-trip, all-or-nothing.

This is the success-path CLEAR of the fleet ``/resume`` route (PR 1b T6). The
route first probes preconditions in Python (read the manifest, derive the feed
universe, check verdicts + reconciled markers), then hands this script the
fully-derived key lists so it can ATOMICALLY re-verify them and clear the halt
keyset only if every check still holds — closing the monitor-death race between
the Python probe and the clear.

``KEYS`` (in order, segmented by the ARGV counts):

1. ``manifest`` keys (``ARGV[1]`` of them) — :func:`data_freshness_manifest_key`
   for every active deployment. Each MUST still EXIST (a monitor that died after
   the Python probe lets its manifest TTL lapse → abort). EMPTY manifests count:
   their key still exists while the monitor is alive.
2. ``verdict`` keys (``ARGV[2]`` of them) — the per-feed ``:verdict`` companion
   (:data:`VERDICT_KEY_SUFFIX`) for every manifest feed across all deployments.
   Each MUST EXIST and equal the bare string ``warm`` (no JSON parsing in Lua —
   the monitor publishes the plain verdict precisely so this stays a bare GET
   compare).
3. ``reconciled`` keys (``ARGV[3]`` of them) — :func:`reconciled_key` for every
   active deployment. Each MUST still EXIST.
4. ``delete`` keys (the remainder, ``#KEYS`` minus the above) — the halt keyset
   to DELETE on success: latch, ``:set_by``, ``:set_at``, cause, cause
   ``:history``, plus the legacy ``:reason`` / ``:source`` transition-compat
   keys. Deleted ONLY after all checks pass.

``ARGV`` (in order):

1. ``n_manifest``   — count of manifest keys at the head of ``KEYS``
2. ``n_verdict``    — count of verdict keys following the manifest keys
3. ``n_reconciled`` — count of reconciled keys following the verdict keys

Returns the bare string ``OK`` on a successful clear (nothing deleted on
failure), or a ``<REASON>:<offending-key>`` string when a check fails — the
route maps any non-``OK`` return to a 409. Reason prefixes: ``MANIFEST_MISSING``
(a monitor died mid-resume), ``VERDICT_MISSING`` / ``VERDICT_NOT_WARM`` (a feed
re-staled or its row expired between probe and clear), ``RECONCILED_MISSING``
(a node lost its reconciliation marker). A feed re-staling AFTER a successful
clear is a NEW stale event the monitor re-halts within a tick.
"""
