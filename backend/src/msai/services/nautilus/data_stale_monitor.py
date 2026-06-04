"""In-node data-stale monitor — the real-money safety core (PR 1b Task 3).

A background async loop that runs INSIDE the live trading subprocess, drives
the pure-domain staleness :func:`~msai.services.live.data_freshness.evaluate`
over a shared :class:`~msai.services.live.data_freshness.FreshnessRegistry` on
a fixed tick, publishes per-feed freshness state to Redis for the API to read,
and LATCHES a fleet halt the moment a required Databento feed/dataset goes
stale past its grace budget.

Setting the fleet halt latch (:func:`~msai.core.halt_keys.fleet_halt_key`) is
all it takes to stop new opening orders fleet-wide: the PR-2/F6 node-side order
gate reads that latch with ≤2s bounded staleness and blocks any new opening
order — so this monitor needs NO changes to the gate, the supervisor, or the
router. Reduce-only / ``MARKET_EXIT`` flatten orders stay allowed (kill-all /
drain flatten still works under the halt). Resume is OPERATOR-ONLY: the monitor
keeps publishing warm verdicts after a feed recovers but never clears the latch.

Lifecycle (mirrors :class:`~msai.services.nautilus.disconnect_handler.IBDisconnectHandler`):

* :meth:`start` — open the monitor's OWN aioredis client via the injected
  factory, publish the required-feed MANIFEST (with TTL), and spawn the loop
  task. A freshness-INELIGIBLE node (empty ``required_feeds``) still runs and
  publishes an EMPTY manifest (distinct from absent).
* the loop calls :meth:`_tick` every ``interval`` seconds: re-set the manifest,
  evaluate, publish per-feed JSON + verdict, and fire the halt on the FIRST
  stale finding. The loop NEVER raises out (an evaluation error is logged and
  swallowed — a freshness bug must not crash the live node).
* :meth:`stop` — cancel the loop, DELETE the manifest + per-feed keys the
  monitor owns, and close the Redis client.

Redis client discipline: the monitor opens its own ``decode_responses=False``
client (mirroring ``_real_disconnect_handler_factory`` in
``trading_node_subprocess.py``); all values are plain JSON / ASCII strings so
they round-trip with the API's text client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from msai.core.halt_keys import (
    HALT_SET_BACKOFF_S,
    HALT_SET_MAX_ATTEMPTS,
    HALT_TTL_SECONDS,
    HALT_WRITE_LUA,
    VERDICT_KEY_SUFFIX,
    HaltCause,
    data_freshness_key,
    data_freshness_manifest_key,
    data_stale_halts_key,
    fleet_halt_write_args,
)
from msai.services.live.data_freshness import (
    FeedObservation,
    GraceConfig,
    SessionPhase,
    StaleFinding,
    asset_class_for_dataset,
    evaluate,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from msai.services.live.data_freshness import FeedKey, FreshnessRegistry

log = logging.getLogger(__name__)

_NS_PER_S = 1_000_000_000


class DataStaleMonitor:
    """In-node Databento feed-freshness monitor + auto-halt.

    Args:
        registry: shared :class:`FreshnessRegistry` written by the
            :class:`~msai.services.nautilus.data_freshness_actor.DataFreshnessActor`.
            May be ``None`` in tests that inject a snapshot directly via
            ``_snapshot_override``.
        required_feeds: the manifest source — the SET of required
            :class:`FeedKey`. EMPTY for a freshness-ineligible node.
        redis_factory: zero-arg callable returning the monitor's OWN aioredis
            client (``decode_responses=False``). Called once at :meth:`start`.
        cfg: :class:`GraceConfig` (grace + tick cadence).
        phase_resolver: ``(asset_class, now_utc) -> SessionPhase`` — injected so
            tests can pin the phase.
        deployment_id: scopes the manifest + per-feed Redis keys.
        account_id / node_id: stamped into the cause + per-feed JSON.
        clock: object with ``timestamp_ns() -> int`` (the node clock in prod;
            a stub in tests).
    """

    def __init__(
        self,
        *,
        registry: FreshnessRegistry | None,
        required_feeds: set[FeedKey],
        redis_factory: Callable[[], Any],
        cfg: GraceConfig,
        phase_resolver: Callable[[Any, datetime], SessionPhase],
        deployment_id: str,
        account_id: str,
        node_id: str,
        clock: Any,
    ) -> None:
        self._registry = registry
        self._required_feeds = set(required_feeds)
        self._redis_factory = redis_factory
        self._cfg = cfg
        self._phase_resolver = phase_resolver
        self._deployment_id = deployment_id
        self._account_id = account_id
        self._node_id = node_id
        self._clock = clock

        self._redis: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._monitor_started_ns: int = 0

        # Idempotency: once we fire the halt we don't re-fire while still stale.
        self._halt_fired: bool = False
        # Keys the monitor has published this lifetime (deleted on clean stop).
        self._owned_feed_keys: set[str] = set()
        # Test seam: when set, the tick evaluates this instead of the registry.
        self._snapshot_override: dict[FeedKey, FeedObservation] | None = None

    # -- LIFECYCLE -------------------------------------------------------

    async def start(self, *, tick_interval_s: float | None = None, run_loop: bool = True) -> None:
        """Open the Redis client, publish the manifest, and spawn the loop.

        ``tick_interval_s`` overrides the loop cadence (defaults to
        ``cfg.interval_s``) — tests pass a tiny value to drive the real loop
        quickly. ``run_loop=False`` opens Redis + publishes the manifest but
        does NOT spawn the loop, so a test (or caller) can step the monitor a
        cycle at a time via :meth:`_tick` without the background loop ticking
        concurrently.
        """
        self._redis = self._redis_factory()
        self._monitor_started_ns = self._now_ns()
        self._stop_event.clear()

        await self._publish_manifest()

        if run_loop:
            interval = (
                tick_interval_s if tick_interval_s is not None else float(self._cfg.interval_s)
            )
            self._task = asyncio.create_task(self._run(interval))

    async def stop(self) -> None:
        """Cancel the loop, delete owned keys, and close the Redis client."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        await self._cleanup_owned_keys()

        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    async def _run(self, interval_s: float) -> None:
        """Main loop. Ticks every ``interval_s`` until :meth:`stop` is called.

        Never raises out — :meth:`_tick` already swallows its own errors, and
        this wrapper guards the sleep path too."""
        while not self._stop_event.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_s)
            except TimeoutError:
                continue

    # -- TICK ------------------------------------------------------------

    async def _tick(self) -> None:
        """One monitor cycle: re-set the manifest, evaluate, publish, and fire
        the halt on the first stale finding. Catches and logs everything — the
        loop must never crash the live node over a freshness-observation bug."""
        try:
            now_ns = self._now_ns()
            snapshot = self._current_snapshot()
            findings = evaluate(
                self._required_feeds,
                snapshot,
                now_ns,
                self._monitor_started_ns,
                self._cfg,
                self._phase_resolver,
            )

            # Pipeline ALL per-tick state writes (manifest SET + per-feed JSON
            # SET + per-feed :verdict SET) into ONE round-trip. Same keys /
            # values / TTLs as the prior sequential awaits. transaction=False:
            # these are independent SETs with no read-modify-write, so MULTI's
            # atomicity buys nothing and a plain command batch is cheaper.
            stale_feeds = {f.feed_key for f in findings if f.feed_key is not None}
            pipe = self._redis.pipeline(transaction=False)
            self._queue_manifest(pipe)
            self._queue_per_feed(pipe, snapshot, stale_feeds, now_ns)
            await pipe.execute()

            if findings:
                if not self._halt_fired:
                    await self._fire_halt(self._select_finding(findings))
            else:
                # Fully-warm tick: the CURRENT stale episode (if any) is over.
                # Re-arm so a LATER stale episode in the same node lifetime
                # re-latches the fleet — the flag tracks the CURRENT episode,
                # not "ever fired". This keeps the within-episode idempotency
                # (no re-LPUSH spam while a feed stays stale) intact while
                # closing the fail-OPEN gap after an operator /resume.
                self._halt_fired = False
        except Exception:  # noqa: BLE001 — a freshness bug must not crash the node
            log.exception(
                "data_stale_monitor_tick_failed",
                extra={"deployment_id": self._deployment_id, "node_id": self._node_id},
            )

    def _current_snapshot(self) -> dict[FeedKey, FeedObservation]:
        if self._snapshot_override is not None:
            return self._snapshot_override
        if self._registry is not None:
            return self._registry.snapshot()
        return {}

    @staticmethod
    def _select_finding(findings: list[StaleFinding]) -> StaleFinding:
        """Pick the cause finding. Prefer a dataset-granularity finding when a
        whole dataset has stalled (a full disconnect / partial-dataset stall
        attributes to the DATASET); otherwise use the first feed finding (a
        single-symbol stall attributes to the FEED)."""
        for f in findings:
            if f.granularity == "dataset":
                return f
        return findings[0]

    # -- MANIFEST --------------------------------------------------------

    async def _publish_manifest(self) -> None:
        """Publish the required-feed manifest standalone (used by :meth:`start`
        before the loop begins). The per-tick path queues the SAME write onto
        the shared pipeline via :meth:`_queue_manifest`."""
        await self._redis.set(
            data_freshness_manifest_key(self._deployment_id),
            json.dumps(self._manifest_payload()),
            ex=self._key_ttl_s(),
        )

    def _manifest_payload(self) -> list[dict[str, str]]:
        """The manifest JSON body: a list of ``{dataset, feed, symbol}``. An
        empty ``required_feeds`` yields ``[]`` (distinct from an absent
        key = monitor-never-started)."""
        return [
            {"dataset": fk.dataset, "feed": fk.native_bar_type_str, "symbol": fk.symbol}
            for fk in self._required_feeds
        ]

    def _queue_manifest(self, pipe: Any) -> None:
        """Queue the manifest SET onto *pipe* (TTL = 3× the tick interval)."""
        pipe.set(
            data_freshness_manifest_key(self._deployment_id),
            json.dumps(self._manifest_payload()),
            ex=self._key_ttl_s(),
        )

    # -- PER-FEED PUBLISH ------------------------------------------------

    def _queue_per_feed(
        self,
        pipe: Any,
        snapshot: dict[FeedKey, FeedObservation],
        stale_feeds: set[FeedKey],
        now_ns: int,
    ) -> None:
        """Queue per-feed freshness JSON + the plain-string verdict companion
        for every required feed onto *pipe*, EVERY tick so the API sees monitor
        liveness.

        Three monitor-published verdicts (Codex iter-2 P1):

        * ``warm`` — fresh data has actually been observed for this feed and it
          is within budget. RESUMABLE.
        * ``pending`` — the feed has NO observation yet but is still within
          startup grace (``evaluate`` has not flagged it stale). The feed is
          NOT yet stale, but neither has any data arrived — so it is NOT
          resumable. After a node restart DURING an ongoing data outage this
          keeps an operator from clearing the halt before any data has landed.
        * ``stale`` — past budget (an observed feed went silent, OR an
          unobserved feed exceeded startup grace). NOT resumable.

        ``missing`` is NOT emitted here — it is API-derived
        (manifest-minus-live-keys). ``missing`` (no row at all) stays distinct
        from ``pending`` (monitor alive, feed simply unobserved so far)."""
        published_at = datetime.now(UTC).isoformat()
        ttl = self._key_ttl_s()
        for fk in self._required_feeds:
            obs = snapshot.get(fk)
            if fk in stale_feeds:
                verdict = "stale"
            elif obs is None:
                # No observation yet, but within startup grace (not stale) →
                # pending: warm-but-unobserved is fail-closed for /resume.
                verdict = "pending"
            else:
                verdict = "warm"
            phase = self._phase_for(fk, now_ns)
            payload = {
                "last_event_ts": obs.ts_event_ns if obs is not None else None,
                "last_arrival_ts": obs.ts_arrival_ns if obs is not None else None,
                "verdict": verdict,
                "phase": phase.phase,
                "grace_s": self._cfg.grace_s(asset_class_for_dataset(fk.dataset), phase.phase),
                "account_id": self._account_id,
                "node_id": self._node_id,
                "symbol": fk.symbol,
                "published_at": published_at,
            }
            key = data_freshness_key(self._deployment_id, fk.dataset, fk.native_bar_type_str)
            pipe.set(key, json.dumps(payload), ex=ttl)
            pipe.set(key + VERDICT_KEY_SUFFIX, verdict, ex=ttl)
            self._owned_feed_keys.add(key)
            self._owned_feed_keys.add(key + VERDICT_KEY_SUFFIX)

    def _phase_for(self, fk: FeedKey, now_ns: int) -> SessionPhase:
        return self._phase_resolver(asset_class_for_dataset(fk.dataset), _ns_to_utc(now_ns))

    # -- HALT WRITE ------------------------------------------------------

    async def _fire_halt(self, finding: StaleFinding) -> None:
        """Latch the fleet halt via the atomic Lua script, with retry/backoff.

        One atomic round-trip writes the latch + ``:set_by`` / ``:set_at``
        companions, sets the cause ONLY-IF-ABSENT (preserving a pre-existing
        manual ``/kill-all`` cause), and LPUSH/LTRIMs the cause onto the capped
        history list. Fires the metric + a single critical log once per
        warm→stale transition, then sets ``_halt_fired`` so subsequent ticks
        don't re-fire while the feed stays stale."""
        cause_json = json.dumps(self._build_cause(finding))
        set_at = datetime.now(UTC).isoformat()
        set_by = f"data_stale_monitor:{self._node_id}"

        keys, argv = fleet_halt_write_args(
            set_by=set_by,
            set_at=set_at,
            cause_json=cause_json,
            ttl_s=HALT_TTL_SECONDS,
        )

        success = False
        last_exc: Exception | None = None
        for attempt in range(HALT_SET_MAX_ATTEMPTS):
            try:
                await self._redis.eval(HALT_WRITE_LUA, len(keys), *keys, *argv)
                success = True
                break
            except Exception as exc:  # noqa: BLE001 — retry transient Redis errors
                last_exc = exc
                log.critical(
                    "data_stale_halt_write_failed",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": HALT_SET_MAX_ATTEMPTS,
                        "deployment_id": self._deployment_id,
                        "node_id": self._node_id,
                    },
                    exc_info=exc,
                )
                if attempt + 1 < HALT_SET_MAX_ATTEMPTS:
                    await asyncio.sleep(HALT_SET_BACKOFF_S * (2**attempt))

        if not success:
            log.critical(
                "data_stale_halt_write_exhausted",
                extra={
                    "deployment_id": self._deployment_id,
                    "node_id": self._node_id,
                    "last_error": str(last_exc),
                },
            )
            return  # leave _halt_fired False so the next tick retries the write

        # One transition log + metric (idempotent while stale thereafter).
        self._halt_fired = True
        log.critical(
            "data_stale_halt_fired",
            extra={
                "deployment_id": self._deployment_id,
                "node_id": self._node_id,
                "account_id": self._account_id,
                "dataset": finding.dataset,
                "feed": finding.feed_key.native_bar_type_str if finding.feed_key else None,
                "symbol": finding.symbol,
                "granularity": finding.granularity,
            },
        )
        # The in-process registry counter lives in THIS subprocess and is
        # invisible to the API /metrics render registry. The Redis counter
        # (SCANned by hydrate_data_health_metrics into the account-labeled
        # msai_data_stale_halts_total series) is the source of truth; the
        # in-process inc is kept as a best-effort local signal. A Redis blip
        # here must not crash the tick — the halt is already latched.
        with contextlib.suppress(Exception):
            await self._redis.incr(data_stale_halts_key(self._account_id))

        from msai.services.observability.trading_metrics import DATA_STALE_HALTS

        DATA_STALE_HALTS.inc(account=self._account_id)

    def _build_cause(self, finding: StaleFinding) -> dict[str, Any]:
        """Canonical cause JSON (see :data:`HALT_WRITE_LUA` docstring)."""
        return {
            "reason": HaltCause.DATA_STALE.value,
            "account_id": self._account_id,
            "node_id": self._node_id,
            "deployment_id": self._deployment_id,
            "dataset": finding.dataset,
            "feed": finding.feed_key.native_bar_type_str if finding.feed_key else None,
            "symbol": finding.symbol,
            "detected_at": _ns_to_utc(finding.detected_at).isoformat(),
            "last_event_ts": finding.last_event_ts,
        }

    # -- CLEANUP ---------------------------------------------------------

    async def _cleanup_owned_keys(self) -> None:
        """Delete the manifest + every per-feed key the monitor published."""
        if self._redis is None:
            return
        keys = [data_freshness_manifest_key(self._deployment_id), *self._owned_feed_keys]
        with contextlib.suppress(Exception):
            await self._redis.delete(*keys)
        self._owned_feed_keys.clear()

    # -- HELPERS ---------------------------------------------------------

    def _now_ns(self) -> int:
        return int(self._clock.timestamp_ns())

    def _key_ttl_s(self) -> int:
        """Per-feed + manifest TTL = 3× the tick interval (a SIGKILLed node's
        keys self-expire). Floor at 1s so a sub-second test interval still
        yields a positive TTL."""
        return max(1, 3 * self._cfg.interval_s)


def _ns_to_utc(now_ns: int) -> datetime:
    return datetime.fromtimestamp(now_ns / _NS_PER_S, tz=UTC)
