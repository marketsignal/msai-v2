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
from dataclasses import dataclass, field
from time import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from msai.core.halt_keys import (
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


@dataclass
class DataHealthSnapshot:
    """Full manifest-first data-feed health for the active fleet."""

    feeds: list[FeedHealth] = field(default_factory=list)
    fleet_halted: bool = False
    halt_cause: dict[str, Any] | None = None
    pacing_by_account: dict[str, int] = field(default_factory=dict)
    stale_halts_by_account: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "feeds": [f.as_dict() for f in self.feeds],
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


async def _redis_mget(redis: Any, keys: list[str]) -> list[str | None]:
    """MGET a batch of keys in one round-trip, decoding each value.

    Returns a list positionally aligned with *keys* (``None`` for an absent
    key). An empty *keys* short-circuits. Any failure degrades to all-``None``
    so a metrics/health read never crashes the caller."""
    if not keys:
        return []
    try:
        raws = await redis.mget(keys)
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_redis_mget_failed", extra={"count": len(keys)})
        return [None] * len(keys)
    return [_decode(raw) for raw in raws]


async def _read_manifest(redis: Any, deployment_id: str) -> list[dict[str, Any]] | None:
    """Read + parse a deployment's required-feed manifest.

    Returns the (possibly empty) feed list, or ``None`` when the manifest is
    absent (monitor never started / node gone + TTL lapsed) or malformed."""
    raw = await _redis_get(redis, data_freshness_manifest_key(deployment_id))
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("data_health_manifest_malformed", extra={"deployment_id": deployment_id})
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


async def _read_fleet_halt(redis: Any) -> tuple[bool, dict[str, Any] | None]:
    try:
        halted = bool(await redis.exists(fleet_halt_key()))
    except Exception:  # noqa: BLE001
        log.warning("data_health_halt_exists_failed")
        return False, None
    cause: dict[str, Any] | None = None
    raw_cause = await _redis_get(redis, halt_cause_key("fleet"))
    if raw_cause is not None:
        try:
            parsed = json.loads(raw_cause)
            if isinstance(parsed, dict):
                cause = parsed
        except (ValueError, TypeError):
            cause = None
    return halted, cause


async def _scan_account_counters(redis: Any, *, prefix: str, match: str) -> dict[str, int]:
    """Scan a ``{prefix}{account_id}`` integer-counter keyspace into
    ``{account_id: count}``.

    SCAN (not KEYS) so a large keyspace doesn't block Redis. Any failure
    degrades to an empty map — a missing metric series is acceptable; a 500 is
    not."""
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
        return {}

    # One MGET for every scanned counter instead of a GET per key.
    values = await _redis_mget(redis, scanned_keys)
    for key_str, raw in zip(scanned_keys, values, strict=True):
        if raw is None:
            continue
        account_id = key_str[len(prefix) :]
        try:
            out[account_id] = int(raw)
        except (ValueError, TypeError):
            continue
    return out


async def _read_pacing_counters(redis: Any) -> dict[str, int]:
    """Scan the IB exec pacing counters into ``{account_id: count}``."""
    return await _scan_account_counters(redis, prefix=_PACING_KEY_PREFIX, match=_PACING_SCAN_MATCH)


async def _read_data_stale_halts_counters(redis: Any) -> dict[str, int]:
    """Scan the data-stale-halt counters into ``{account_id: count}``."""
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
) -> list[FeedHealth]:
    """Manifest-first per-feed health for one deployment.

    For each manifest feed, look up its published per-feed JSON. Present → use
    its verdict + fields; absent/expired → derive a ``missing`` verdict. A
    deployment with an absent/malformed manifest contributes no rows."""
    manifest = await _read_manifest(redis, deployment_id)
    if manifest is None:
        return []

    # Collect the valid manifest entries + their per-feed keys, then MGET every
    # per-feed JSON row in ONE round-trip (instead of a GET per feed).
    entries: list[tuple[str, str, Any]] = []  # (dataset, feed, manifest_symbol)
    feed_keys: list[str] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        dataset = str(entry.get("dataset", ""))
        feed = str(entry.get("feed", ""))
        if not dataset or not feed:
            continue
        entries.append((dataset, feed, entry.get("symbol")))
        feed_keys.append(data_freshness_key(deployment_id, dataset, feed))

    raw_rows = await _redis_mget(redis, feed_keys)

    rows: list[FeedHealth] = []
    for (dataset, feed, manifest_symbol), raw_row in zip(entries, raw_rows, strict=True):
        if raw_row is None:
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
                verdict=str(payload.get("verdict") or "missing"),
                published_at=payload.get("published_at"),
            )
        )
    return rows


async def collect_data_health(db: AsyncSession, redis: Any) -> DataHealthSnapshot:
    """Build the manifest-first data-feed health snapshot for the active fleet.

    Enumerates ACTIVE deployments (same 5-tuple ``ACTIVE_DEPLOYMENT_STATUSES``
    as ``/resume``), reads each deployment's manifest + per-feed rows, derives
    ``missing`` for any declared-but-undelivered feed, and attaches the fleet
    halt latch + parsed cause + the per-account IB exec-pacing counters.

    Degrades gracefully: a DB error yields an empty snapshot (logged), so the
    ``/metrics`` scrape never 500s on a transient DB blip."""
    snapshot = DataHealthSnapshot()

    snapshot.fleet_halted, snapshot.halt_cause = await _read_fleet_halt(redis)
    snapshot.pacing_by_account = await _read_pacing_counters(redis)
    snapshot.stale_halts_by_account = await _read_data_stale_halts_counters(redis)

    try:
        result = await db.execute(
            select(LiveDeployment).where(LiveDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES))
        )
        deployments = result.scalars().all()
    except Exception:  # noqa: BLE001 — a metrics/health read must never crash the caller
        log.warning("data_health_active_deployments_query_failed")
        return snapshot

    for dep in deployments:
        rows = await _read_deployment_feeds(
            redis,
            deployment_id=str(dep.id),
            fallback_account_id=dep.account_id,
        )
        snapshot.feeds.extend(rows)
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
    account that left the snapshot disappears from the next scrape."""
    from msai.services.observability.trading_metrics import (
        DATA_FEED_AGE_SECONDS,
        DATA_FEED_STALE,
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

    DATA_FEED_AGE_SECONDS.replace_children(age_samples)
    DATA_FEED_STALE.replace_children(stale_samples)
    DATABENTO_DATASET_ALIVE.replace_children(alive_samples)
    IB_EXEC_PACING_ERRORS.replace_children(pacing_samples)
    DATA_STALE_HALTS.replace_children(stale_halt_samples)


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
