"""In-node Databento feed-freshness observer (PR 1b Task 2).

This module wires the pure-domain :class:`FreshnessRegistry`
(:mod:`msai.services.live.data_freshness`) into a live Nautilus
:class:`TradingNode` via a Nautilus :class:`Actor`. The actor's only
job is **observation**: it subscribes to the same NATIVE Databento bar
types the strategies already consume (via the symbology shim) and
records each bar's event/arrival timestamps into a shared
:class:`FreshnessRegistry`. A later task (PR 1b Task 4) drives the
staleness :func:`~msai.services.live.data_freshness.evaluate` over that
registry on a timer and latches a halt when a required feed goes stale.

Why an Actor, and why a SEPARATE one from the shim
--------------------------------------------------
The node already wires :class:`~msai.services.symbology_shim_actor.SymbologyShimActor`
to bridge native↔canonical bar topics. This actor is intentionally
distinct so freshness observation is decoupled from the retag bridge:
the shim retags, this actor only watches. Subscribing to an
already-strategy-subscribed EXTERNAL bar type does NOT create a
duplicate upstream Databento subscription — the data engine only calls
``client.subscribe_bars`` when not already subscribed
(``nautilus_trader/data/engine.pyx:1191``); the message bus fans the
bars out to every subscriber. So this actor's ``on_start``
subscriptions are free observation taps.

Config boundary
---------------
:class:`DataFreshnessActorConfig` is a Nautilus :class:`ActorConfig`
and therefore round-trips through ``ImportableActorConfig`` via
``msgspec`` — so it carries PRIMITIVES ONLY (lists / dicts / strings).
The shared :class:`FreshnessRegistry` is a live, non-serializable
object that CANNOT cross that boundary; it is injected
POST-``node.build()`` via :meth:`DataFreshnessActor.set_registry`
(same setter API shape as ``SymbologyShimActor.set_audit_sink``).
The actor constructs its OWN internal :class:`FreshnessRegistry` at
init and ALWAYS records into it from the first bar, so bars delivered
between node start and the post-build injection are never lost;
``set_registry`` REPLAYS the internal snapshot into the shared registry
and then switches to it. There is always a registry — the actor never
crashes, even if ``set_registry`` is never called.

Import discipline
-----------------
Importing ``nautilus_trader`` installs the uvloop event-loop policy at
import time (``.claude/rules/nautilus.md`` gotcha #1). That is fine in
the live subprocess (which already imports Nautilus) and in tests, but
it is why the pure-domain
:mod:`msai.services.live.data_freshness` deliberately avoids the
import — THIS module is allowed to import Nautilus because it only
runs inside the TradingNode.
"""

from __future__ import annotations

import threading

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import Bar, BarType

from msai.services.live.data_freshness import FeedKey, FreshnessRegistry


class DataFreshnessActorConfig(ActorConfig, frozen=True):
    """Configuration schema for :class:`DataFreshnessActor`.

    Inherits from Nautilus :class:`ActorConfig` so the kernel's
    ``ActorFactory.create`` path can ``msgspec``-deserialize an
    ``ImportableActorConfig.config`` dict into a typed config.

    PRIMITIVES ONLY — the kernel reconstructs the actor from a
    serialized config dict, so no live objects may live here.

    Attributes:
        native_bar_types: NATIVE Databento bar-type strings (e.g.
            ``"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"``) to subscribe to
            and observe. These mirror the shim's native subscriptions;
            subscribing here is a free observation tap (no duplicate
            upstream Databento subscription).
        bar_type_datasets: Map from each native bar-type string to its
            Databento dataset (e.g.
            ``{"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "EQUS.MINI"}``).
            Derived ONCE at config assembly in ``live_node_config`` from
            ``canonical_to_native_bar_types`` + ``venue_dataset_map`` so
            the actor never has to re-derive the dataset ad hoc.
    """

    native_bar_types: list[str] = []
    bar_type_datasets: dict[str, str] = {}


class DataFreshnessActor(Actor):  # type: ignore[misc]  # Actor is a Cython class without typeshed; mypy --strict can't see the subclass contract.
    """Observe native Databento bar feeds and record their freshness.

    Lifecycle:

    * ``__init__`` keeps an explicitly-typed reference to the config so
      mypy sees the concrete schema, and constructs the actor's OWN
      internal :class:`FreshnessRegistry` so recording starts from the
      first bar (replayed into the shared registry on :meth:`set_registry`).
    * :meth:`on_start` subscribes to each configured native bar type via
      ``self.subscribe_bars``. The data engine routes these to the
      Databento client; the message bus fans the bars out (no duplicate
      upstream subscription — see module docstring).
    * :meth:`on_bar` records the bar's EVENT time (``bar.ts_event``) and
      ARRIVAL time (``self.clock.timestamp_ns()``) into the shared
      registry under the right :class:`FeedKey`.
    """

    def __init__(self, config: DataFreshnessActorConfig) -> None:
        super().__init__(config=config)
        # ``config`` is also stored on ``self.config`` by the base class,
        # but we keep an explicitly-typed reference so mypy can see the
        # concrete schema without narrowing on every read.
        self._fresh_config: DataFreshnessActorConfig = config

        # FIX 1(a): the actor constructs its OWN registry at init and ALWAYS
        # records into it from the very first bar. ``set_registry(shared)``
        # later REPLAYS this internal registry's snapshot into the shared one
        # and switches ``self._registry`` to point at the shared registry.
        #
        # Why: the real monitor factory injects the shared registry only after
        # the node has started (``trading_node_subprocess.py`` — monitor wiring
        # runs after ``wait_until_ready`` and BEFORE ``_mark_running`` per the
        # iter-8 reorder), so bars delivered between node start and that
        # injection would be silently LOST if the actor no-opped until then.
        # There is now ALWAYS a registry — the
        # warn-once "no registry" path is gone. If ``set_registry`` is never
        # called, the actor simply keeps recording into its internal registry
        # (it never crashes).
        self._registry: FreshnessRegistry = FreshnessRegistry()

        # FIX 1 (iter 8 — registry handoff race, FINAL closure): an
        # actor-level lock that mutually excludes ``on_bar``'s record from the
        # ENTIRE ``set_registry`` handoff (swap → snapshot → replay). The
        # swap-first ordering alone only NARROWED the race: ``on_bar`` reads
        # ``self._registry`` into a local, then calls ``record`` — a handler
        # that read the OLD reference just before the swap can still call
        # ``record`` on the discarded internal registry AFTER ``set_registry``
        # has already taken ``old.snapshot()``, so that observation never
        # reaches the shared registry and is LOST. With both sides holding this
        # lock, no record can interleave the swap/snapshot/replay — the race is
        # closed COMPLETELY, not merely narrowed. The lock is uncontended on the
        # hot path (the registry itself already locks per record, so this is a
        # second, bounded, normally-uncontended acquire per bar).
        self._handoff_lock = threading.Lock()

        # Hot-path map: BarType -> FeedKey, precomputed once in ``on_start`` so
        # ``on_bar`` is a single dict lookup (no per-bar ``str(bar.bar_type)``
        # and no per-bar FeedKey allocation). Populated only for bar types that
        # have a known dataset in ``bar_type_datasets``.
        self._bar_type_to_feedkey: dict[BarType, FeedKey] = {}

    # -- REGISTRY INJECTION ----------------------------------------------

    def set_registry(self, registry: FreshnessRegistry | None) -> None:
        """Install the shared :class:`FreshnessRegistry` after build.

        The registry is a live, non-serializable object so it can't be
        part of the persisted ``ActorConfig``; the subprocess wires it
        in after the kernel has instantiated the actor via
        ``ActorFactory.create``. Mirrors
        ``SymbologyShimActor.set_audit_sink``.

        FIX 1(a): this REPLAYS every observation recorded into the internal
        registry (bars that arrived between node start and this injection) into
        the shared one via ``shared.record`` — so no early bar is lost.
        ``record`` keeps the newest event ts per feed, so a feed that later
        receives a fresher bar is unaffected by the replay.

        FIX 1 (iter 8 — FINAL closure of the handoff race): the ENTIRE handoff
        (swap ``self._registry`` → snapshot the OLD internal registry → replay
        that snapshot into the shared registry) runs under
        :attr:`_handoff_lock`, the SAME lock :meth:`on_bar` holds while it
        records. With both sides locked, no ``record`` can interleave the
        swap/snapshot/replay: a bar handler that read the OLD ``self._registry``
        reference just before the swap blocks on the lock until the snapshot +
        replay have completed, then records into the (now-current) shared
        registry. So an in-flight observation can NEVER be stranded in the
        discarded internal registry after its snapshot was taken — the race is
        closed completely, not merely narrowed. ``record`` is newest-wins, so
        the order of the handoff bar vs the replayed snapshot doesn't matter;
        the freshest event ts per feed always survives.

        Swap-first ordering is retained inside the lock for clarity (so any
        record that DID slip in before the lock was acquired this side sees the
        shared registry), but correctness no longer depends on it — the lock is
        the guarantee.

        Passing ``None`` is a no-op (keeps recording into the internal
        registry) — the actor never crashes.
        """
        if registry is None:
            return
        with self._handoff_lock:
            # Swap FIRST: repoint at the shared registry. Hold the old registry
            # to replay its backlog. Both the swap and the replay happen under
            # the lock, so ``on_bar`` cannot snapshot-then-record into the old
            # registry concurrently — it is fully serialized against this block.
            old = self._registry
            self._registry = registry
            # Replay the old internal observations into the shared registry.
            # ``record`` is newest-wins, so this is safe even if the shared
            # registry already holds a fresher row for a feed.
            for feed_key, obs in old.snapshot().items():
                registry.record(feed_key, obs.ts_event_ns, obs.ts_arrival_ns)

    # -- LIFECYCLE -------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to every configured native bar type at startup.

        Each ``subscribe_bars`` is an observation tap — the data engine
        only triggers a fresh upstream Databento subscription when the
        bar type isn't already subscribed (by the strategy / shim);
        otherwise the message bus fans the existing stream out to this
        actor too.
        """
        datasets = self._fresh_config.bar_type_datasets
        for native_str in self._fresh_config.native_bar_types:
            bar_type = BarType.from_str(native_str)
            self.subscribe_bars(bar_type)
            # Precompute the FeedKey for every bar type with a known dataset so
            # ``on_bar`` is a single dict lookup. A bar type with no dataset is
            # left out of the map (``on_bar`` records nothing for it — same as
            # the prior unmapped-dataset drop path, minus the per-bar warning,
            # since the drift is now detectable at startup if a dataset is
            # absent here).
            dataset = datasets.get(native_str)
            if dataset is not None:
                self._bar_type_to_feedkey[bar_type] = FeedKey(
                    dataset=dataset, native_bar_type_str=native_str
                )

    # -- BAR HANDLER -----------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Record an observed bar's event + arrival timestamps.

        Hot path: a single dict lookup of the bar type in the
        :attr:`_bar_type_to_feedkey` map precomputed in :meth:`on_start`
        (no per-bar ``str(bar.bar_type)`` and no per-bar :class:`FeedKey`
        allocation), then ``registry.record``. EVENT time is the bar's own
        ``ts_event``; ARRIVAL time is the actor clock's current ns.

        FIX 1(a): there is ALWAYS a registry (the actor constructs its own at
        init; ``set_registry`` later swaps in the shared one and replays). So
        every bar is recorded from the first event — no warn-once no-op path.
        A bar type absent from the map (no dataset derived at startup — config
        drift) records nothing. It never raises — a freshness-observation
        failure must never crash the live node.

        FIX 1 (iter 8): the registry-pointer read AND the ``record`` happen
        together under :attr:`_handoff_lock`, the same lock
        :meth:`set_registry` holds for its whole handoff. This makes the
        read-then-record atomic with respect to the swap/snapshot/replay, so
        this observation can never be stranded in a registry that was already
        snapshotted and discarded. Uncontended fast path: the lock is normally
        free (the handoff happens once at startup), so the extra acquire per bar
        is bounded and cheap.
        """
        feed_key = self._bar_type_to_feedkey.get(bar.bar_type)
        if feed_key is None:
            # No dataset was derived for this bar type at startup (config
            # drift). Record nothing under a bogus dataset; never crash.
            return

        # Snapshot the arrival clock OUTSIDE the lock (it's independent of the
        # registry pointer), then take the lock to read the pointer + record
        # atomically against ``set_registry``'s handoff.
        ts_arrival = self.clock.timestamp_ns()
        with self._handoff_lock:
            self._registry.record(feed_key, bar.ts_event, ts_arrival)
