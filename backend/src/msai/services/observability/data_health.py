"""Shared data-feed health reader + Prometheus hydration (PR 1b T7).

ONE manifest-first reader feeds BOTH surfaces so they can never drift:

* ``GET /api/v1/live/data-health`` (operator JSON window), and
* the Prometheus ``/metrics`` scrape path (``DATA_FEED_AGE_SECONDS`` /
  ``DATA_FEED_STALE`` / ``DATABENTO_DATASET_ALIVE`` / ``IB_EXEC_PACING_ERRORS`` /
  ``DATA_STALE_HALTS``).

Manifest-FIRST means: for every ACTIVE deployment we read the monitor's
required-feed manifest (the SET of feeds the node DECLARED it must keep warm),
then look up each feed's published freshness row. A manifest feed with NO live
row is reported with a derived ``missing`` verdict — never silently absent — so
an operator sees the feed the node expected but never delivered. A feed WITH a
live row carries the monitor's verbatim verdict — ``warm`` (fresh data
observed), ``pending`` (monitor alive but no data observed yet, within startup
grace), or ``stale`` (past budget). ``pending`` (monitor-published, row
present) stays distinct from ``missing`` (no row at all).

The reader uses the SAME wire formats the in-node monitor publishes
(``services/nautilus/data_stale_monitor.py``):

* manifest = JSON list of ``{dataset, feed, symbol}`` (``feed`` = native bar type)
* per-feed key = JSON ``{last_event_ts, last_arrival_ts, verdict, phase, grace_s,
  account_id, node_id, symbol, published_at}``

All reads go through a TEXT (``decode_responses=True``) Redis client.

Redis discipline (Codex-safe): every Redis call is wrapped — a Redis outage at
scrape time must degrade gracefully (render whatever is registered) and NEVER
500 the scrape or the route. A single deployment whose manifest is malformed is
skipped, not fatal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from time import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from msai.core.halt_keys import (
    VERDICT_KEY_SUFFIX,
    data_freshness_key,
    data_freshness_manifest_key,
    fleet_halt_key,
    halt_cause_key,
)
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.services.live.broker_account_service import ACTIVE_DEPLOYMENT_STATUSES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

_NS_PER_S = 1_000_000_000

_PACING_KEY_PREFIX = "msai:metrics:ib_exec_pacing:"
_PACING_SCAN_MATCH = _PACING_KEY_PREFIX + "*"

_STALE_HALTS_KEY_PREFIX = "msai:metrics:data_stale_halts:"
_STALE_HALTS_SCAN_MATCH = _STALE_HALTS_KEY_PREFIX + "*"


@dataclass(frozen=True)
class FeedHealth:
    """One required-feed freshness row as the API + metrics see it."""

    account_id: str | None
    node_id: str | None
    deployment_id: str
    dataset: str
    feed: str  # native bar-type string
    symbol: str | None
    last_event_ts: int | None
    phase: str | None
    grace_s: int | None
    verdict: str  # warm | pending | stale | missing
    published_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "node_id": self.node_id,
            "deployment_id": self.deployment_id,
            "dataset": self.dataset,
            "feed": self.feed,
            "symbol": self.symbol,
            "last_event_ts": self.last_event_ts,
            "phase": self.phase,
            "grace_s": self.grace_s,
            "verdict": self.verdict,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class MonitorMissing:
    """A RUNNING deployment whose data-stale monitor is missing/dead (FIX 4).
    (The alert keys off ``status == "running"`` only — see the running-only
    rationale at the collection site; the ENUMERATION below scans the broader
    active set for feed rendering.)

    ``reason`` is ``'manifest absent'`` (no manifest key at all — monitor never
    started or its node died + TTL lapsed) or ``'manifest malformed'`` (the key
    exists but isn't a parseable JSON list).

    A production deployment ALWAYS runs the monitor, so ``monitor_missing`` for
    a ``running`` row signals a genuine fault (crashed monitor / TTL lapse). The
    ``TradingNodePayload.data_freshness_enabled=False`` switch is a TEST-ONLY
    escape hatch with no production override path — a legacy / no-feed node
    still runs the monitor and publishes the EMPTY manifest (rendered as
    vacuous-warm, never paged). A False-flag deployment would publish NO
    manifest and so WOULD page here (and fail ``/resume`` closed); that is the
    intended fail-closed posture for an unsupported configuration, not a false
    page. See ``TradingNodePayload.data_freshness_enabled`` for the full
    rationale."""

    deployment_id: str
    account_id: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "account_id": self.account_id,
            "reason": self.reason,
        }


@dataclass
class DataHealthSnapshot:
    """Full manifest-first data-feed health for the active fleet."""

    feeds: list[FeedHealth] = field(default_factory=list)
    monitor_missing: list[MonitorMissing] = field(default_factory=list)
    fleet_halted: bool = False
    halt_cause: dict[str, Any] | None = None
    pacing_by_account: dict[str, int] = field(default_factory=dict)
    stale_halts_by_account: dict[str, int] = field(default_factory=dict)
    # FIX 3 (review iter-16 P2): True when the collection ran cleanly end-to-end.
    # ANY DB *or* Redis read FAILURE during ``collect_data_health`` sets this
    # False so the metric hydration can DISTINGUISH "collection failed" (don't
    # touch the gauges — leave the previous children standing so Prometheus
    # alerts stay red/stale instead of vanishing) from "fleet is legitimately
    # empty" (replace with the empty set — a legit prune). Without this flag a
    # transient DB *or* Redis blip wiped every data-feed gauge and reset all
    # stale/halt alerts to absent.
    #
    # FIX (review iter-17, Codex P1 / pr-toolkit P2): the flag now covers EVERY
    # Redis read, not just the DB query. Each pre-DB Redis read (fleet-halt,
    # pacing counters, stale-halt counters) and each per-deployment manifest read
    # flips ``collection_ok=False`` on an exception. The per-surface route
    # payload still degrades gracefully (fleet-halt → unhalted, counters → empty
    # map, manifest read-failure → deployment skipped) so the JSON window + the
    # ``monitor_missing`` page semantics are UNCHANGED — only the hydration guard
    # changes: a Redis blip during ANY of these reads now leaves the prior gauge
    # children standing instead of pruning them with a partial/empty snapshot.
    collection_ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "feeds": [f.as_dict() for f in self.feeds],
            "monitor_missing": [m.as_dict() for m in self.monitor_missing],
            "fleet_halted": self.fleet_halted,
            "halt_cause": self.halt_cause,
        }


def _decode(raw: Any) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def _redis_get(redis: Any, key: str) -> str | None:
    try:
        raw = await redis.get(key)
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_redis_get_failed", extra={"key": key})
        return None
    return _decode(raw)


async def _redis_mget_checked(redis: Any, keys: list[str]) -> tuple[list[str | None], bool]:
    """MGET a batch of keys in one round-trip, decoding each value, AND report
    whether the MGET succeeded.

    Returns ``(values, ok)`` where ``values`` is positionally aligned with
    *keys* (``None`` for an absent key). ``ok`` is False when the MGET RAISED
    (the values degrade to all-``None`` so a metrics/health read never crashes
    the caller). Callers that feed metric gauges use ``ok`` to fail the
    collection so a Redis blip leaves the prior gauge children standing instead
    of pruning them with an empty snapshot (review iter-17 + iter-18). An empty
    *keys* short-circuits to ``([], True)``."""
    if not keys:
        return [], True
    try:
        raws = await redis.mget(keys)
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_redis_mget_failed", extra={"count": len(keys)})
        return [None] * len(keys), False
    return [_decode(raw) for raw in raws], True


# Sentinel reason emitted when the manifest GET itself raised (transient Redis
# outage) — DISTINCT from a genuine absence. FIX 1: a read-failure must NOT be
# reported as monitor_missing (it is "unknown", not "dead") so a Redis blip
# never makes every active monitor look dead and page on-call. The caller skips
# a read-failed deployment entirely (no rows, no monitor_missing, no gauge).
_MANIFEST_READ_FAILED = "manifest read failed"


async def _read_manifest(redis: Any, deployment_id: str) -> tuple[list[Any] | None, str | None]:
    """Read + parse a deployment's required-feed manifest.

    Returns ``(manifest_list, None)`` on success — the (possibly empty) feed
    list. On failure returns ``(None, reason)`` where ``reason`` is one of:

    * ``'manifest absent'`` — the key genuinely does not exist (monitor never
      started / node gone + TTL lapsed). The caller surfaces this as
      monitor_missing.
    * ``'manifest malformed'`` — key exists but isn't a parseable JSON list.
      Also monitor_missing.
    * ``_MANIFEST_READ_FAILED`` — the Redis GET itself raised (transient
      outage). FIX 1: this is NOT a genuine absence — "unknown", not "dead". The
      caller SKIPS the deployment (no rows, no monitor_missing, no gauge) so a
      Redis blip never pages every active monitor as dead.

    FIX 4: the absent/malformed distinction is preserved so the monitor-missing
    surface can name WHY a deployment's monitor is dark."""
    # Read the manifest key DIRECTLY (not via ``_redis_get``) so a raised
    # exception is distinguishable from a genuine ``None`` (absent key).
    try:
        raw_any = await redis.get(data_freshness_manifest_key(deployment_id))
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_manifest_read_failed", extra={"deployment_id": deployment_id})
        return None, _MANIFEST_READ_FAILED
    raw = _decode(raw_any)
    if raw is None:
        return None, "manifest absent"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("data_health_manifest_malformed", extra={"deployment_id": deployment_id})
        return None, "manifest malformed"
    if not isinstance(parsed, list):
        return None, "manifest malformed"
    return parsed, None


async def _read_fleet_halt(redis: Any) -> tuple[bool, dict[str, Any] | None, bool]:
    """Read the fleet halt latch + parsed cause.

    Returns ``(halted, cause, ok)``. ``ok`` is False when the ``EXISTS`` probe
    RAISED — the route payload still degrades to ``(False, None)`` (unhalted) so
    the JSON window never 500s, but ``ok=False`` lets the caller flip
    ``collection_ok`` so hydration skips the gauge prune on a Redis blip (review
    iter-17). A malformed cause JSON is NOT a read failure (``ok`` stays True) —
    the latch read itself succeeded."""
    try:
        halted = bool(await redis.exists(fleet_halt_key()))
    except Exception:  # noqa: BLE001
        log.warning("data_health_halt_exists_failed")
        return False, None, False
    cause: dict[str, Any] | None = None
    raw_cause = await _redis_get(redis, halt_cause_key("fleet"))
    if raw_cause is not None:
        try:
            parsed = json.loads(raw_cause)
            if isinstance(parsed, dict):
                cause = parsed
        except (ValueError, TypeError):
            cause = None
    return halted, cause, True


async def _scan_account_counters(
    redis: Any, *, prefix: str, match: str
) -> tuple[dict[str, int], bool]:
    """Scan a ``{prefix}{account_id}`` integer-counter keyspace into
    ``{account_id: count}``.

    SCAN (not KEYS) so a large keyspace doesn't block Redis. Returns
    ``(counters, ok)``. ``ok`` is False when the SCAN or the follow-up MGET
    RAISED — the route payload still degrades to an empty map (a missing metric
    series is acceptable; a 500 is not), but ``ok=False`` lets the caller flip
    ``collection_ok`` so hydration skips the gauge prune on a Redis blip (review
    iter-17). A genuinely empty keyspace returns ``({}, True)`` — distinct from a
    failed scan, so an empty fleet still prunes."""
    out: dict[str, int] = {}
    scanned_keys: list[str] = []
    try:
        async for key in redis.scan_iter(match=match):
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            account_id = key_str[len(prefix) :]
            if not account_id:
                continue
            scanned_keys.append(key_str)
    except Exception:  # noqa: BLE001
        log.warning("data_health_counter_scan_failed", extra={"prefix": prefix})
        return {}, False

    # One MGET for every scanned counter instead of a GET per key. A raised MGET
    # degrades to all-``None`` AND is reported as a read failure so the caller
    # can fail the collection (the counters would otherwise silently drop to
    # empty and prune the firing pacing/halt gauges).
    values, mget_ok = await _redis_mget_checked(redis, scanned_keys)
    for key_str, raw in zip(scanned_keys, values, strict=True):
        if raw is None:
            continue
        account_id = key_str[len(prefix) :]
        try:
            out[account_id] = int(raw)
        except (ValueError, TypeError):
            continue
    return out, mget_ok


async def _read_pacing_counters(redis: Any) -> tuple[dict[str, int], bool]:
    """Scan the IB exec pacing counters into ``({account_id: count}, ok)``."""
    return await _scan_account_counters(redis, prefix=_PACING_KEY_PREFIX, match=_PACING_SCAN_MATCH)


async def _read_data_stale_halts_counters(redis: Any) -> tuple[dict[str, int], bool]:
    """Scan the data-stale-halt counters into ``({account_id: count}, ok)``."""
    return await _scan_account_counters(
        redis, prefix=_STALE_HALTS_KEY_PREFIX, match=_STALE_HALTS_SCAN_MATCH
    )


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


async def _read_deployment_feeds(
    redis: Any,
    *,
    deployment_id: str,
    fallback_account_id: str | None,
) -> tuple[list[FeedHealth], str | None, bool]:
    """Manifest-first per-feed health for one deployment.

    For each manifest feed, look up its published per-feed JSON (metadata) AND
    its plain-string ``:verdict`` companion (the VERDICT value). Both present →
    use the companion's verdict + the JSON's fields; JSON present but companion
    absent → ``missing`` (partial-write drift, fail-visible — matches what
    ``/resume`` does); JSON row absent → ``missing``.

    VERDICT SOURCE (review iter-16 P2): the verdict is read from the SAME
    ``data_freshness_key(...) + VERDICT_KEY_SUFFIX`` companion that ``/resume``
    trusts (``api/live.py``), NOT from the JSON row's embedded ``verdict`` field.
    The monitor writes the JSON row and the companion as two SEPARATE SETs in a
    non-transactional pipeline, so they CAN drift on a partial write — reading
    the verdict from the JSON row could show ``warm`` on data-health while
    ``/resume`` blocks off a ``stale``/absent companion. Both surfaces now read
    the SAME companion so they can never contradict on the verdict.

    Returns ``(rows, monitor_missing_reason, ok)``. FIX 4: a deployment with an
    absent/malformed manifest contributes NO feed rows AND a non-``None``
    reason (``'manifest absent'`` / ``'manifest malformed'``) so the caller can
    surface it under ``monitor_missing`` rather than silently dropping it.
    Codex iter-12 P2: ``'manifest malformed'`` now also covers a LIST manifest
    with malformed ENTRIES (non-dict, or missing dataset/feed) — matching
    ``/resume``'s fail-closed read so the two surfaces never contradict.
    FIX 1: a manifest READ-FAILURE returns the ``_MANIFEST_READ_FAILED`` reason
    so the caller can SKIP the deployment entirely (a transient Redis error is
    "unknown", not "dead").

    FIX (review iter-18, pr-toolkit P1 / Codex P2): the per-feed JSON-row + the
    ``:verdict`` companion MGETs now go through ``_redis_mget_checked`` and the
    success flag is threaded out as the third tuple element ``ok``. The manifest
    GET can succeed while EITHER MGET RAISES (a Redis blip between the calls — a
    pool-checkout failure / cluster timeout on the larger MGET). Previously the
    rows + verdicts were read via an UNCHECKED MGET wrapper, which swallowed
    the ok flag: a raised MGET degraded every feed to all-``None`` →
    ``missing``-verdict rows while ``collection_ok`` stayed True, so the next
    ``/metrics`` hydration pruned/rewrote the live feed gauges (age children
    pruned, ``stale``→1, ``alive``→0) on the blip — exactly the blip-wipe the
    ``collection_ok`` contract promises to prevent. ``ok=False`` lets the caller
    flip ``collection_ok=False`` so hydration leaves the prior gauges standing.
    The degraded rows still flow into the route payload UNCHANGED (the JSON
    window + ``monitor_missing`` semantics are untouched — hydration-guard
    only)."""
    manifest, missing_reason = await _read_manifest(redis, deployment_id)
    if manifest is None:
        # A manifest READ-FAILURE is already flagged via the reason
        # (``_MANIFEST_READ_FAILED``); an absent/malformed manifest is a genuine
        # (non-Redis-failure) state. ``ok=True`` here — the per-feed MGET below
        # never ran, so there is no MGET failure to report from this branch.
        return [], missing_reason, True

    # Collect the manifest entries + their per-feed keys, then MGET every
    # per-feed JSON row in ONE round-trip (instead of a GET per feed).
    #
    # Codex iter-12 P2 — entry-level malformed detection aligned with /resume.
    # A LIST manifest whose ENTRIES are malformed (a non-dict entry, or a dict
    # missing 'dataset'/'feed') is treated as a malformed DEPLOYMENT-level
    # manifest — exactly as ``/resume`` (api/live.py) reads it: any malformed
    # entry blocks the resume as 'manifest malformed'. Previously this loop
    # SKIPPED such entries, so a running deployment with manifest ``[{}]``
    # rendered ``feeds:[]`` with NO monitor_missing + no gauge while /resume
    # 409'd the SAME manifest — contradictory operator surfaces. Now both fail
    # closed at the deployment level: return the 'manifest malformed' reason so
    # the caller surfaces it under ``monitor_missing`` (+ gauge).
    entries: list[tuple[str, str, Any]] = []  # (dataset, feed, manifest_symbol)
    feed_keys: list[str] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            log.warning("data_health_manifest_malformed", extra={"deployment_id": deployment_id})
            # ``ok=True`` — no MGET ran in this branch (matches the dict-missing-
            # field malformed branch below). Returning the OLD 2-tuple here made
            # the 3-value-unpacking call site raise ``ValueError`` → 500 on BOTH
            # ``/live/data-health`` and ``/metrics`` (review iter-19, pr-toolkit P0).
            return [], "manifest malformed", True
        # Validate RAW values BEFORE coercion: ``str(None)`` would yield the
        # truthy string "None" and let a ``{"dataset": null, ...}`` entry pass
        # here while /resume rejects the same manifest as malformed (Codex
        # iter-13 P2 — the two surfaces must fail closed identically).
        dataset = entry.get("dataset")
        feed = entry.get("feed")
        if not isinstance(dataset, str) or not isinstance(feed, str) or not dataset or not feed:
            log.warning("data_health_manifest_malformed", extra={"deployment_id": deployment_id})
            return [], "manifest malformed", True
        entries.append((dataset, feed, entry.get("symbol")))
        feed_keys.append(data_freshness_key(deployment_id, dataset, feed))

    # FIX 4: MGET the per-feed JSON rows AND their ``:verdict`` companions in
    # parallel. The JSON row is the METADATA source (timestamps / phase / grace /
    # symbol / published_at); the companion is the VERDICT source (the SAME key
    # ``/resume`` trusts), so the two surfaces never contradict on the verdict.
    #
    # FIX (review iter-18): both MGETs go through ``_redis_mget_checked`` so a
    # raised MGET (Redis blip between the manifest GET and the feed read) is
    # reported via ``ok`` instead of silently degrading every feed to ``missing``
    # while ``collection_ok`` stays True. The degraded all-``None`` rows still
    # flow into the route payload as ``missing`` (unchanged), but ``ok=False`` is
    # threaded out so the caller flips ``collection_ok`` and hydration leaves the
    # prior gauges standing.
    verdict_keys = [k + VERDICT_KEY_SUFFIX for k in feed_keys]
    raw_rows, rows_ok = await _redis_mget_checked(redis, feed_keys)
    raw_verdicts, verdicts_ok = await _redis_mget_checked(redis, verdict_keys)
    ok = rows_ok and verdicts_ok

    rows: list[FeedHealth] = []
    for (dataset, feed, manifest_symbol), raw_row, raw_verdict in zip(
        entries, raw_rows, raw_verdicts, strict=True
    ):
        if raw_row is None:
            # No JSON row at all → missing (regardless of companion).
            rows.append(
                FeedHealth(
                    account_id=fallback_account_id,
                    node_id=None,
                    deployment_id=deployment_id,
                    dataset=dataset,
                    feed=feed,
                    symbol=manifest_symbol,
                    last_event_ts=None,
                    phase=None,
                    grace_s=None,
                    verdict="missing",
                    published_at=None,
                )
            )
            continue
        try:
            payload = json.loads(raw_row)
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # FIX 4: the verdict comes from the COMPANION, not the JSON row. JSON
        # present but companion absent = partial-write drift → 'missing'
        # (fail-visible, matching what /resume would do — block). A present
        # companion supplies the verbatim verdict (warm/pending/stale).
        verdict = raw_verdict if raw_verdict else "missing"
        rows.append(
            FeedHealth(
                account_id=payload.get("account_id") or fallback_account_id,
                node_id=payload.get("node_id"),
                deployment_id=deployment_id,
                dataset=dataset,
                feed=feed,
                symbol=payload.get("symbol") or manifest_symbol,
                last_event_ts=_coerce_int(payload.get("last_event_ts")),
                phase=payload.get("phase"),
                grace_s=_coerce_int(payload.get("grace_s")),
                verdict=verdict,
                published_at=payload.get("published_at"),
            )
        )
    return rows, None, ok


async def collect_data_health(db: AsyncSession, redis: Any) -> DataHealthSnapshot:
    """Build the manifest-first data-feed health snapshot for the active fleet.

    Enumerates ACTIVE deployments (same 5-tuple ``ACTIVE_DEPLOYMENT_STATUSES``
    as ``/resume``), reads each deployment's manifest + per-feed rows, derives
    ``missing`` for any declared-but-undelivered feed, and attaches the fleet
    halt latch + parsed cause + the per-account IB exec-pacing counters.

    Degrades gracefully: a DB query error yields an empty snapshot, and ANY
    Redis read failure (the pre-DB fleet-halt / pacing / stale-halt reads OR a
    per-deployment manifest read) is reported via ``collection_ok=False``
    (logged) — the per-surface route payload still degrades (unhalted /
    empty-map / deployment-skipped) so the ``/metrics`` scrape never 500s on a
    transient DB *or* Redis blip. FIX 3 + iter-17: ``collection_ok=False`` signals
    hydration to LEAVE the previously-hydrated gauges standing (don't prune to
    empty on a transient DB/Redis failure) — see
    :func:`hydrate_metrics_from_snapshot`. A fully-successful but empty
    collection still prunes (``collection_ok`` stays True)."""
    snapshot = DataHealthSnapshot()

    # FIX (review iter-17): each pre-DB Redis read reports an ``ok`` flag. Its
    # degrade-to-empty return value still feeds the route payload (so the JSON
    # window never 500s), but a raised read flips ``collection_ok=False`` so
    # hydration leaves the prior gauges standing on a Redis blip.
    snapshot.fleet_halted, snapshot.halt_cause, halt_ok = await _read_fleet_halt(redis)
    snapshot.pacing_by_account, pacing_ok = await _read_pacing_counters(redis)
    snapshot.stale_halts_by_account, stale_ok = await _read_data_stale_halts_counters(redis)
    if not (halt_ok and pacing_ok and stale_ok):
        snapshot.collection_ok = False

    try:
        result = await db.execute(
            select(LiveDeployment).where(LiveDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES))
        )
        deployments = result.scalars().all()
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_active_deployments_query_failed")
        # FIX 3: a FAILED enumeration is NOT a legitimately-empty fleet — mark it
        # so hydration skips the gauge prune (leave prior children standing).
        snapshot.collection_ok = False
        return snapshot

    for dep in deployments:
        deployment_id = str(dep.id)
        rows, missing_reason, feeds_ok = await _read_deployment_feeds(
            redis,
            deployment_id=deployment_id,
            fallback_account_id=dep.account_id,
        )
        # FIX (review iter-24 P2 — false-green node attribution): a manifest-
        # declared feed with NO published row is emitted with node_id=None (the
        # node never published it). Hydration groups ``msai_databento_dataset_alive``
        # by ``(account, node, dataset)``, so a missing feed (node→"unknown") and
        # a warm feed (node="node-1") on the SAME dataset/deployment split into
        # TWO node buckets — node-1 alive=1 + unknown alive=0 — making a
        # node-scoped dashboard filtered to node-1 read FALSE GREEN while a feed
        # of that exact deployment+node is dead. All feeds of a deployment live on
        # ONE node by construction (the manifest is per-deployment-node), so any
        # present row's node_id is THE node for every missing row in the same
        # deployment. Inherit it so the missing feed lands in the SAME
        # dataset_alive bucket (→ alive=0, correctly dead). The SAME FeedHealth
        # objects feed the route JSON, so the route's missing entries also show
        # the inherited node_id (consistency — one source of truth). If NO row in
        # the deployment carried a node_id (fully missing / fully quiet), the
        # deployment is genuinely unattributable: keep node_id=None ("unknown")
        # — the dead signal stays visible as dataset_alive=0 for the
        # (account,"unknown",dataset) bucket.
        deployment_node_id = next((r.node_id for r in rows if r.node_id is not None), None)
        if deployment_node_id is not None:
            rows = [
                replace(r, node_id=deployment_node_id) if r.node_id is None else r for r in rows
            ]
        snapshot.feeds.extend(rows)
        # FIX (review iter-18): a per-feed MGET failure (manifest GET succeeded
        # but the feed-row / verdict MGET RAISED) degrades the rows to ``missing``
        # in the route payload (unchanged) but MUST flip ``collection_ok=False``
        # so hydration leaves the prior gauge children standing instead of
        # pruning/rewriting them with the degraded snapshot. This covers the last
        # unchecked Redis read inside ``_read_deployment_feeds``.
        if not feeds_ok:
            snapshot.collection_ok = False
        if missing_reason is None:
            continue
        # FIX 1: a manifest READ-FAILURE (transient Redis error) is "unknown",
        # NOT "dead" — skip the deployment entirely (no rows already; no
        # monitor_missing, no gauge). A Redis blip must never page every active
        # monitor as dead.
        #
        # FIX (review iter-17): the deployment is still skipped from the route
        # payload + monitor_missing page (unchanged), but a read-failure for ANY
        # deployment now ALSO flips ``collection_ok=False`` — otherwise the
        # skipped deployment contributes NO feed rows and hydration would prune
        # that deployment's previously-firing gauge children with a partial
        # snapshot. Leaving the gauges standing on a Redis blip is the
        # documented contract.
        if missing_reason == _MANIFEST_READ_FAILED:
            snapshot.collection_ok = False
            continue
        # FIX 2: the monitor_missing ALERT is restricted to status == 'running'.
        # The in-node monitor publishes its manifest BEFORE the node marks
        # itself running (the monitor's ``start()`` runs ahead of
        # ``_mark_running`` in ``trading_node_subprocess.py``), and on a clean
        # stop it does NOT eagerly delete the manifest — it lets the key
        # self-expire on its 3×-tick TTL (Codex iter-10 P2). INVARIANT: the
        # manifest's lifetime ⊇ [pre-``running``, ~3×tick past ``stop()``], so a
        # ``running`` row ALWAYS implies the manifest is present — both during
        # healthy startup (monitor starts first) AND through the shutdown window
        # (the monitor stops before the terminal write flips the row out of
        # ``running``, but the manifest stays TTL-alive). A running deployment
        # with NO manifest is therefore a dead monitor, not a startup OR
        # shutdown race. A deployment in a normal lifecycle status
        # (building / starting / ready / stopping) with no manifest is EXPECTED.
        # The ``status == 'running'`` guard is a clean, race-free signal at both
        # lifecycle edges. Alerting on the non-running statuses would page
        # on-call during every normal startup and shutdown.
        #
        # ASYMMETRY (intentional): /resume's fail-closed gate (api/live.py)
        # covers the FULL active set (ACTIVE_DEPLOYMENT_STATUSES) — it refuses
        # to resume any active deployment it cannot prove warm, which is correct
        # because resume is an operator-driven safety action. The monitor_missing
        # ALERT here is narrower (running only) because its job is to page on a
        # dead monitor, not to gate a state transition.
        if dep.status != "running":
            continue
        # FIX 4: a RUNNING deployment with an absent/malformed manifest is a
        # monitor-missing/dead case — surface it explicitly so it is
        # distinguishable from a quiet (empty) fleet.
        snapshot.monitor_missing.append(
            MonitorMissing(
                deployment_id=deployment_id,
                account_id=dep.account_id,
                reason=missing_reason,
            )
        )
    return snapshot


def _age_seconds(last_event_ts: int | None, *, now_s: float) -> float | None:
    if last_event_ts is None:
        return None
    age = now_s - (last_event_ts / _NS_PER_S)
    return age if age >= 0 else 0.0


def hydrate_metrics_from_snapshot(snapshot: DataHealthSnapshot) -> None:
    """REPLACE the data-feed health gauge children from *snapshot*.

    Imported lazily-at-call so this module stays import-cheap. Each gauge's
    children are swapped wholesale (``Gauge.replace_children``) so a feed/dataset/
    account that left the snapshot disappears from the next scrape.

    FIX 3 (review iter-16 P2) + iter-17: if the snapshot's ``collection_ok`` is
    False the collection hit a transient DB *or* Redis failure — either the
    active-deployment DB query, a pre-DB Redis read (fleet-halt / pacing /
    stale-halt), or a per-deployment manifest read. This is NOT a
    legitimately-empty fleet. SKIP the replace entirely so the
    previously-hydrated children stay standing: Prometheus alerts then stay
    red/stale through the blip instead of momentarily vanishing (which would
    clear a firing stale/halt/pacing alert). A SUCCESSFUL collection that happens
    to be empty still replaces with the empty set (a legitimate prune)."""
    if not snapshot.collection_ok:
        log.warning("data_health_skip_hydrate_collection_failed")
        return

    from msai.services.observability.trading_metrics import (
        DATA_FEED_AGE_SECONDS,
        DATA_FEED_STALE,
        DATA_MONITOR_MISSING,
        DATA_STALE_HALTS,
        DATABENTO_DATASET_ALIVE,
        IB_EXEC_PACING_ERRORS,
    )

    now_s = time()

    age_samples: list[tuple[dict[str, str], float]] = []
    stale_samples: list[tuple[dict[str, str], float]] = []
    # dataset-granularity: alive iff EVERY feed in the (account,node,dataset) is warm.
    dataset_all_warm: dict[tuple[str, str, str], bool] = {}

    for feed in snapshot.feeds:
        labels = {
            "account": feed.account_id or "unknown",
            "node": feed.node_id or "unknown",
            "dataset": feed.dataset,
            "symbol": feed.symbol or "unknown",
            "feed": feed.feed,
        }
        is_stale = feed.verdict != "warm"
        stale_samples.append((labels, 1.0 if is_stale else 0.0))
        age = _age_seconds(feed.last_event_ts, now_s=now_s)
        if age is not None:
            age_samples.append((labels, age))

        dkey = (labels["account"], labels["node"], feed.dataset)
        dataset_all_warm[dkey] = dataset_all_warm.get(dkey, True) and (not is_stale)

    alive_samples: list[tuple[dict[str, str], float]] = [
        (
            {"account": account, "node": node, "dataset": dataset},
            1.0 if all_warm else 0.0,
        )
        for (account, node, dataset), all_warm in dataset_all_warm.items()
    ]

    pacing_samples: list[tuple[dict[str, str], float]] = [
        ({"account": account}, float(count))
        for account, count in snapshot.pacing_by_account.items()
    ]

    stale_halt_samples: list[tuple[dict[str, str], float]] = [
        ({"account": account}, float(count))
        for account, count in snapshot.stale_halts_by_account.items()
    ]

    # FIX 4: 1 per monitor-missing deployment, labeled by deployment. Pruned
    # via replace_children like the others, so a deployment whose monitor came
    # back (manifest present again) disappears from the next scrape.
    monitor_missing_samples: list[tuple[dict[str, str], float]] = [
        ({"deployment": m.deployment_id}, 1.0) for m in snapshot.monitor_missing
    ]

    DATA_FEED_AGE_SECONDS.replace_children(age_samples)
    DATA_FEED_STALE.replace_children(stale_samples)
    DATABENTO_DATASET_ALIVE.replace_children(alive_samples)
    IB_EXEC_PACING_ERRORS.replace_children(pacing_samples)
    DATA_STALE_HALTS.replace_children(stale_halt_samples)
    DATA_MONITOR_MISSING.replace_children(monitor_missing_samples)


async def hydrate_data_health_metrics(db: AsyncSession, redis: Any) -> DataHealthSnapshot:
    """Collect the snapshot AND hydrate the gauges in one call.

    Invoked from BOTH the data-health route and the ``/metrics`` render path so
    the two surfaces share one reader. Returns the snapshot so the route can
    serialize it without a second read. Never raises — degrades to whatever the
    reader produced."""
    snapshot = await collect_data_health(db, redis)
    try:
        hydrate_metrics_from_snapshot(snapshot)
    except Exception:  # noqa: BLE001 — metrics hydration must never 500 the caller
        log.warning("data_health_metric_hydration_failed")
    return snapshot
