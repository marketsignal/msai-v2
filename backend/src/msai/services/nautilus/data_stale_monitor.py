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
  factory, publish the required-feed MANIFEST (with TTL), SYNCHRONOUSLY publish
  the CURRENT per-feed rows + ``:verdict`` companions (an inline first
  evaluation — ``pending`` for an unobserved feed at start), and spawn the loop
  task. Publishing the per-feed state BEFORE the loop (and BEFORE
  ``_mark_running`` in the run-loop ordering) OVERWRITES any stale ``warm``
  verdict leftovers from a prior run on the same stable deployment id, closing
  the fast-restart resume-on-stale-verdict window (Codex iter-11 High). A
  freshness-INELIGIBLE node (empty ``required_feeds``) still runs and publishes
  an EMPTY manifest (distinct from absent).
* the loop calls :meth:`_tick` every ``interval`` seconds: re-set the manifest,
  evaluate, publish per-feed JSON + verdict, and fire the halt on the FIRST
  stale finding. The loop NEVER raises out (an evaluation error is logged and
  swallowed — a freshness bug must not crash the live node).
* :meth:`stop` — cancel the loop, DELETE the per-feed JSON + ``:verdict`` keys
  (rebuilt from ``required_feeds``), LEAVE the manifest to TTL-expire, and
  close the Redis client. Deleting the per-feed keys removes stale ``warm``
  verdicts so a fast restart can't ``/resume`` off them; deleting ONLY the
  per-feed keys does NOT page ``monitor_missing`` (that keys off the MANIFEST).
  The manifest is left because deleting it on a still-``running`` row was
  iter-10's false-page bug (the subprocess stops the monitor in its ``finally``
  BEFORE the terminal write flips the row out of ``running``); the manifest's
  3×-tick TTL keeps it alive through the shutdown window, then it self-expires.
  Residual (CRASH only): a SIGKILL skips clean stop, leaving old ``warm``
  verdicts ≤3×tick — bounded by the same TTL that bounds all verdict freshness;
  the manifest TTL then fails ``/resume`` closed. Accepted.

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
    from collections.abc import Awaitable, Callable

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
        on_halt: Callable[[], Awaitable[None]] | None = None,
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
        # Optional fail-closed local-shutdown callback (review iter-16 P1).
        # Mirrors :class:`IBDisconnectHandler.on_halt`: fired after a halt
        # ATTEMPT regardless of whether the Redis write succeeded, so a flatten /
        # local-shutdown hook runs even when the halt-latch write exhausts its
        # retries (the F6 order gate fails closed only on its own Redis READ
        # failure, so an asymmetric WRITE failure would otherwise leave the node
        # trading until a later tick's write succeeds). Fires once per stale
        # episode.
        self._on_halt = on_halt

        self._redis: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._monitor_started_ns: int = 0

        # Idempotency: once we fire the halt we don't re-fire while still stale.
        self._halt_fired: bool = False
        # Separate episode-scoped guard for the local-shutdown ``on_halt``
        # callback. The latch-write path can EXHAUST (leaving ``_halt_fired``
        # False so a later tick retries the write), but ``on_halt`` must still
        # fire exactly once per episode — so it tracks its own state and is
        # re-armed on the same fully-warm tick that re-arms ``_halt_fired``.
        self._on_halt_fired: bool = False
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

        Fast-restart safety (Codex iter-11 High): besides the manifest, we
        SYNCHRONOUSLY publish the CURRENT per-feed rows + ``:verdict``
        companions for EVERY required feed BEFORE returning — an inline first
        evaluation (the same evaluate→publish path the loop uses, just once,
        before any background tick). Deployment ids are stable across
        restarts, so a FAST restart can reach ``_mark_running`` + the
        reconciled marker while the OLD run's ``warm`` verdict keys are still
        TTL-alive (clean stop deletes the per-feed keys, but a CRASH leaves
        them ≤3×tick). Because the run-loop ordering starts this monitor
        BEFORE ``_mark_running``, these fresh verdicts (``pending`` for an
        unobserved feed at start) OVERWRITE any stale ``warm`` leftovers
        before the deployment row is ever ``running`` — closing the
        clean/crash-restart window structurally so ``/resume`` can never clear
        a halt off a previous run's verdicts.

        RAISES (Codex iter-12 P1 — fail-closed): if opening Redis, publishing
        the manifest, or publishing the initial per-feed state fails, this
        method RAISES. The run-loop factory path
        (``trading_node_subprocess.run_subprocess_async``) calls ``start()``
        BEFORE ``_mark_running``; a raised error is caught by the run-loop's
        monitor-wiring fail-closed catch-all → the subprocess exits 1 (failed)
        and the deployment is NEVER marked ``running``. The safety envelope
        must be fully established or the node must not run.
        """
        self._redis = self._redis_factory()
        self._monitor_started_ns = self._now_ns()
        self._stop_event.clear()

        await self._publish_manifest()
        await self._publish_initial_feed_state()

        if run_loop:
            interval = (
                tick_interval_s if tick_interval_s is not None else float(self._cfg.interval_s)
            )
            self._task = asyncio.create_task(self._run(interval))

    async def stop(self) -> None:
        """Cancel the loop, DELETE the per-feed keys, and close the Redis client.

        Clean-stop semantics (Codex iter-10 P2 + iter-11 High):

        * **Per-feed JSON + ``:verdict`` keys — EAGERLY DELETED.** We rebuild
          the key list from ``required_feeds`` (the monitor knows its own
          universe) and delete every per-feed freshness key + its ``:verdict``
          companion. WHY (iter-11): deployment ids are stable across restarts,
          so a FAST restart can reach ``_mark_running`` while a prior run's
          ``warm`` verdict keys are still alive — ``/resume`` could then clear
          a halt off STALE warm verdicts before the new monitor observes
          anything. Deleting them on clean stop removes that fuel. (The new
          run's :meth:`start` ALSO overwrites them with fresh ``pending``
          verdicts before ``_mark_running``, so deletion + overwrite are
          belt-and-suspenders.) Deleting ONLY the per-feed keys does NOT
          trigger ``monitor_missing`` — that pages off an ABSENT MANIFEST, not
          off missing per-feed rows. During the shutdown window the affected
          feeds render as ``missing`` (accurate — the monitor is gone), and
          ``/resume`` fired during shutdown FAILS CLOSED (absent verdict =
          blocking), which is correct for a stopping node.

        * **Manifest — LEFT to TTL-expire, NOT deleted.** Deleting the
          manifest was iter-10's false-page bug: the subprocess finally-block
          stops this monitor BEFORE the terminal write flips the
          ``LiveDeployment`` row out of ``running``, so an absent manifest on a
          still-``running`` row would falsely page ``monitor_missing``. The
          manifest is TTL'd at 3× the tick interval (``_key_ttl_s``, ~15s at
          the default cadence); it stays alive through the shutdown window (no
          false page) and self-expires shortly after, by which time the
          terminal write has long flipped the row out of ``running``.

        Residual (CRASH path only — accepted): a SIGKILL / crash skips this
        clean ``stop()``, so the old run's ``warm`` verdict keys survive ≤3×tick
        and the reconciled marker (no TTL) lingers. A ``/resume`` fired in that
        ≤15s post-crash window could accept verdicts up to ≤15s stale — bounded
        by the same TTL that bounds ALL verdict freshness, after which the
        manifest TTL lapses and ``/resume`` fails closed. Accepted: a crash is
        not a clean stop, and the staleness bound is identical to every other
        verdict the system trusts.
        """
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._delete_per_feed_keys()
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    async def _delete_per_feed_keys(self) -> None:
        """DELETE the per-feed JSON + ``:verdict`` companion keys for every
        required feed (clean-stop cleanup — Codex iter-11). Rebuilt from
        ``required_feeds`` so it stays in lockstep with what :meth:`_queue_per_feed`
        publishes. The manifest is deliberately NOT in this list."""
        keys: list[str] = []
        for fk in self._required_feeds:
            base = data_freshness_key(self._deployment_id, fk.dataset, fk.native_bar_type_str)
            keys.append(base)
            keys.append(base + VERDICT_KEY_SUFFIX)
        if keys:
            await self._redis.delete(*keys)

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
        """One monitor cycle: evaluate, FIRE THE HALT FIRST (the safety action),
        then publish telemetry. Catches and logs everything — the loop must
        never crash the live node over a freshness-observation bug.

        SAFETY-FIRST ORDERING (review iter-16 P1): the halt is the real-money
        safety action and MUST be independent of telemetry publishing. Earlier
        this method publatched the manifest/per-feed/verdict pipeline FIRST and
        fired the halt AFTER ``pipe.execute()`` — so a telemetry-pipeline failure
        raised into the outer catch and SKIPPED the halt even though
        ``evaluate()`` had already returned stale findings (a feed could be stale
        AND the fleet stay un-halted because a cosmetic per-feed write blipped).
        Now the order is: evaluate → (if findings and not fired) ``_fire_halt``
        → then publish telemetry inside its OWN try/except so a publish failure
        is logged and swallowed but can NEVER prevent or undo the halt. The halt
        write itself already has its own retry/backoff + exhaustion handling in
        :meth:`_fire_halt`."""
        try:
            now_ns = self._now_ns()
            snapshot = self._current_snapshot()
            findings = await self._evaluate_and_apply_halt(snapshot, now_ns)

            # TELEMETRY SECOND — in its OWN try/except so a publish failure can
            # NEVER affect the halt above. Pipeline ALL per-tick state writes
            # (manifest SET + per-feed JSON SET + per-feed :verdict SET) into
            # ONE round-trip. transaction=False: these are independent SETs with
            # no read-modify-write, so MULTI's atomicity buys nothing and a
            # plain command batch is cheaper.
            try:
                stale_feeds = {f.feed_key for f in findings if f.feed_key is not None}
                pipe = self._redis.pipeline(transaction=False)
                self._queue_manifest(pipe)
                self._queue_per_feed(pipe, snapshot, stale_feeds, now_ns)
                await pipe.execute()
            except Exception:  # noqa: BLE001 — telemetry publish must not affect the halt
                log.exception(
                    "data_stale_monitor_publish_failed",
                    extra={"deployment_id": self._deployment_id, "node_id": self._node_id},
                )
        except Exception:  # noqa: BLE001 — a freshness bug must not crash the node
            log.exception(
                "data_stale_monitor_tick_failed",
                extra={"deployment_id": self._deployment_id, "node_id": self._node_id},
            )

    async def _evaluate_and_apply_halt(
        self, snapshot: dict[FeedKey, FeedObservation], now_ns: int
    ) -> list[StaleFinding]:
        """Evaluate the snapshot and apply the SAFETY ACTION (latch the fleet
        halt on stale findings, or re-arm the episode guards on a fully-warm
        result). Returns the findings so the caller can publish telemetry.

        Extracted from :meth:`_tick` so the SAME safety-first evaluate→fire
        logic runs BOTH on every background tick AND synchronously from
        :meth:`start` (Codex iter-20 P2 — a feed already stale at monitor start
        must halt before ``start()`` returns, not wait for the first background
        tick which races the run loop's ``_mark_running``). The episode
        idempotency (``_halt_fired`` / ``_on_halt_fired``) is shared, so a
        synchronous start-time halt does NOT re-fire on the first tick while the
        feed stays stale.

        Telemetry publishing stays the caller's responsibility — this method is
        purely the halt decision so it can be reused by the start path (which
        publishes its own initial per-feed state) without double-writing."""
        findings = evaluate(
            self._required_feeds,
            snapshot,
            now_ns,
            self._monitor_started_ns,
            self._cfg,
            self._phase_resolver,
        )

        # SAFETY ACTION FIRST — before any telemetry write. A stale finding
        # latches the fleet halt regardless of whether the subsequent
        # telemetry publish succeeds.
        if findings:
            if not self._halt_fired:
                await self._fire_halt(self._select_finding(findings))
        else:
            # Fully-warm: the CURRENT stale episode (if any) is over. Re-arm so
            # a LATER stale episode in the same node lifetime re-latches the
            # fleet — the flag tracks the CURRENT episode, not "ever fired".
            # This keeps the within-episode idempotency (no re-LPUSH spam while
            # a feed stays stale) intact while closing the fail-OPEN gap after
            # an operator /resume.
            self._halt_fired = False
            # Re-arm the local-shutdown callback guard alongside the latch guard
            # so a LATER stale episode fires ``on_halt`` again.
            self._on_halt_fired = False

        return findings

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

    async def _publish_initial_feed_state(self) -> None:
        """Synchronously publish the CURRENT per-feed JSON + ``:verdict``
        companions for every required feed at :meth:`start`, BEFORE the first
        background tick (Codex iter-11 High — fast-restart verdict reuse).

        This runs one inline evaluation/publish over the start-time snapshot
        (empty for an unobserved feed → ``pending``) using the SAME
        evaluate→publish path the loop tick uses, so the fresh verdicts
        OVERWRITE any stale ``warm`` leftovers from a prior run on this stable
        deployment id.

        It ALSO fires the halt synchronously when the start-time snapshot is
        already stale (Codex iter-20 P2): the run loop calls ``start()`` BEFORE
        ``_mark_running``, so a feed that is stale the instant the monitor comes
        up (e.g. a node restart DURING an ongoing outage where the feed had been
        observed then went silent) must latch the fleet halt here — before the
        deployment can flip to ``running`` — rather than waiting for the first
        background tick (which runs CONCURRENTLY with ``_mark_running``). The
        halt decision reuses :meth:`_evaluate_and_apply_halt`, sharing the
        episode-idempotency guards with the loop tick so it does not double-fire.

        FAIL-CLOSED (Codex iter-12 P1): unlike the per-tick loop (which swallows
        its own errors for steady-state resilience), a failure HERE PROPAGATES
        out of :meth:`start`. The initial state is the safety envelope: if it
        is only HALF-established (manifest written, per-feed verdicts NOT), a
        leftover TTL-alive ``warm`` verdict from a prior run on this stable
        deployment id survives, and the run-loop would otherwise reach
        ``_mark_running`` + the reconciled marker — letting ``/resume`` clear a
        halt off that stale verdict (breaking the overwrite-before-running
        invariant). By raising, the run-loop's monitor-wiring fail-closed path
        fails the subprocess BEFORE ``_mark_running``, so the node never trades
        with a half-published envelope. (The loop's per-tick publish error
        swallowing is unchanged — see :meth:`_tick`.)
        """
        now_ns = self._now_ns()
        snapshot = self._current_snapshot()
        # SAFETY ACTION FIRST (Codex iter-20 P2): apply the halt decision over
        # the start-time snapshot SYNCHRONOUSLY, before publishing telemetry and
        # before :meth:`start` returns. A feed already stale at monitor start
        # (e.g. a node restart DURING an ongoing outage where the feed had been
        # observed then went silent) must latch the fleet halt BEFORE the
        # subprocess run loop reaches ``_mark_running`` — otherwise the
        # deployment could flip to ``running`` with KNOWN-stale data and no
        # awaited halt, trading un-halted until the first background tick. The
        # shared episode guards (``_halt_fired`` / ``_on_halt_fired``) mean this
        # start-time fire does NOT re-fire on the first tick while the feed stays
        # stale. Reuses the exact evaluate→fire path the loop tick uses, so there
        # is no duplicated halt logic.
        findings = await self._evaluate_and_apply_halt(snapshot, now_ns)
        stale_feeds = {f.feed_key for f in findings if f.feed_key is not None}
        pipe = self._redis.pipeline(transaction=False)
        self._queue_per_feed(pipe, snapshot, stale_feeds, now_ns)
        await pipe.execute()

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
        don't re-fire while the feed stays stale.

        LOCAL-SHUTDOWN FALLBACK (review iter-16 P1): the optional ``on_halt``
        callback is fired after the halt ATTEMPT REGARDLESS of whether the Redis
        latch write succeeded — mirroring :meth:`IBDisconnectHandler._fire_halt`.
        Rationale: the F6 node-side order gate fails closed only when its OWN
        Redis READS fail; an asymmetric latch-WRITE exhaustion (writes fail but
        reads still work / return None→fail-closed only transiently) would
        otherwise leave the node trading until a later tick's write succeeds. The
        injected ``on_halt`` (in the subprocess factory) sets the local shutdown
        event + stops the node — purely in-process, no Redis — so the node tears
        down even if the latch never lands. Fired at most once per stale episode
        (guarded by ``_on_halt_fired``, re-armed on a fully-warm tick)."""
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
            # Leave _halt_fired False so the next tick retries the WRITE, but
            # STILL fire the local-shutdown callback so the node tears down even
            # though the latch never landed (the F6 read gate can't be trusted to
            # block on an asymmetric write failure). Once per episode.
            await self._maybe_fire_on_halt()
            return

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

        # Local-shutdown callback — fired on the successful-write path too, once
        # per episode, so the on_halt semantics match the disconnect handler
        # (callback fires whenever a halt is triggered, write success or not).
        await self._maybe_fire_on_halt()

    async def _maybe_fire_on_halt(self) -> None:
        """Fire the optional ``on_halt`` local-shutdown callback at most once per
        stale episode. A callback failure is logged and swallowed — the halt
        attempt already ran, and the callback failure is just metadata (mirrors
        :class:`IBDisconnectHandler`)."""
        if self._on_halt is None or self._on_halt_fired:
            return
        self._on_halt_fired = True
        try:
            await self._on_halt()
        except Exception:  # noqa: BLE001 — a callback failure must not crash the loop
            log.exception(
                "data_stale_on_halt_callback_failed",
                extra={"deployment_id": self._deployment_id, "node_id": self._node_id},
            )

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
