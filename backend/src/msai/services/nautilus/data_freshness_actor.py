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
Without an injected registry the actor NO-OPs safely — it records
nothing, logs a single warning, and never crashes the node.

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
      mypy sees the concrete schema, and initialises the injected
      registry to ``None`` (no-op until :meth:`set_registry` is called).
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

        # Shared registry — injected POST-build via ``set_registry``.
        # ``None`` means "no-op safely" (record nothing, warn once).
        self._registry: FreshnessRegistry | None = None

        # Ensures the "no registry" warning is emitted at most once so a
        # high-frequency bar stream can't flood the log.
        self._warned_no_registry: bool = False

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

        Passing ``None`` (or never calling this) leaves the actor in the
        safe no-op state.
        """
        self._registry = registry

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

        Without an injected registry the actor NO-OPs: it logs a single
        warning and returns. A bar type absent from the map (no dataset
        derived at startup — config drift) records nothing. It never
        raises — a freshness-observation failure must never crash the
        live node.
        """
        registry = self._registry
        if registry is None:
            if not self._warned_no_registry:
                self._warned_no_registry = True
                if self.log is not None:
                    self.log.warning(
                        "DataFreshnessActor has no registry injected; "
                        "freshness observation is a no-op (call set_registry "
                        "after node.build()). This bar and subsequent bars are "
                        "NOT being recorded.",
                    )
            return

        feed_key = self._bar_type_to_feedkey.get(bar.bar_type)
        if feed_key is None:
            # No dataset was derived for this bar type at startup (config
            # drift). Record nothing under a bogus dataset; never crash.
            return

        registry.record(feed_key, bar.ts_event, self.clock.timestamp_ns())
