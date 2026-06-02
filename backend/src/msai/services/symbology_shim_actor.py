"""Production hook for the bidirectional Databento ↔ IBKR symbology shim.

This module wires :mod:`msai.services.symbology_shim` into a live
Nautilus :class:`TradingNode` by way of a Nautilus :class:`Actor`. The
actor is the **load-bearing piece** of PR 1 Task T11 — without BOTH
halves wired (outbound subscription + inbound retag), no bars ever
reach the strategy and the multi-account live-trading topology can't
make progress.

Two responsibilities (Codex iter 7 P1 of PR 1):

* **Outbound subscription.** Strategies subscribe to canonical
  ``<SYM>.IBKR-1-MINUTE-LAST-EXTERNAL`` bar types. The Nautilus
  data-engine routes those subscriptions to the data client that owns
  the ``IBKR`` venue — but in the per-account fleet topology, our
  Databento client owns ``XNAS``/``GLBX``/etc. (the NATIVE venues from
  ``venue_dataset_map``). The shim's :meth:`SymbologyShimActor.on_start`
  eagerly subscribes to the corresponding NATIVE bar types so the
  data-engine routes those subscriptions to the Databento client and
  the feed actually starts streaming.

* **Inbound retag.** When Databento publishes native bars onto the
  message bus, the actor's handler calls
  :func:`msai.services.symbology_shim.retag_inbound_bar` to translate
  the venue suffix to ``IBKR``, then republishes the new bar onto the
  canonical bar topic where the strategy's bus subscription picks it
  up. Audit metadata about the original native id is forwarded to a
  caller-supplied sink so the provenance isn't lost in the re-tag.

The actor is a Nautilus 1.223.0 :class:`Actor` subclass — verified
against ``backend/.venv/lib/python3.12/site-packages/nautilus_trader/common/actor.pyx``.
The base class is a Cython ``cdef class``, so we subclass at the Python
boundary by extending the Pure-Python ``Actor`` alias re-exported from
:mod:`nautilus_trader.common.actor`. Bus access primitives
(``self.subscribe_bars``, ``self.msgbus.publish``) come from the base
class once the actor is registered.

Notes
-----
- ``SymbologyShimActorConfig`` is a Nautilus :class:`ActorConfig` so it
  round-trips through ``ImportableActorConfig`` correctly via
  ``ActorFactory.create`` (the kernel uses ``msgspec.json.encode`` to
  serialize the config dict; ``ActorConfig`` inherits from
  ``NautilusConfig`` which provides the ``parse``/``dict`` surface).
- The ``audit_metadata_sink`` field is NOT in the persisted config —
  it's a non-serializable callable so it can't survive ``msgspec``.
  The actor sets a no-op default at construction time; callers that
  want real audit metadata install one via :meth:`set_audit_sink`
  after construction.
- ``ib_login_key`` lands in the config as a passthrough for audit
  symmetry with the supervisor's payload — the actor itself ignores
  it.

PR 1 scope keeps the actor equities-only (the underlying shim is too).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import Bar, BarType

from msai.services.symbology_shim import retag_inbound_bar

if TYPE_CHECKING:
    from collections.abc import Callable


class SymbologyShimActorConfig(ActorConfig, frozen=True):
    """Configuration schema for :class:`SymbologyShimActor`.

    Inherits from Nautilus :class:`ActorConfig` so the kernel's
    ``ActorFactory.create`` path can ``msgspec``-deserialize an
    ``ImportableActorConfig.config`` dict into a typed config.

    Attributes:
        canonical_to_native_bar_types: Mapping from canonical bar-type
            strings (e.g. ``"AAPL.IBKR-1-MINUTE-LAST-EXTERNAL"``) to
            their native Databento counterparts (e.g.
            ``"AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"``). The keys are what
            strategies subscribe to; the values are what
            :meth:`SymbologyShimActor.on_start` subscribes to on their
            behalf so Databento actually streams the data.
        venue_dataset_map: The authoritative ``native_venue → dataset``
            map produced by the upstream symbology resolver (e.g.
            ``{"XNAS": "EQUS.MINI"}``). Used by the inbound handler to
            attach the dataset string to the audit metadata.
        ib_login_key: IB Gateway login key the deployment is bound to.
            Embedded for audit symmetry with the supervisor payload —
            the actor does not consume it directly.
    """

    canonical_to_native_bar_types: dict[str, str] = {}
    venue_dataset_map: dict[str, str] = {}
    ib_login_key: str = ""


class SymbologyShimActor(Actor):  # type: ignore[misc]  # Actor is a Cython class without typeshed; mypy --strict can't see the subclass contract.
    """Two-way bridge between canonical IBKR bar topics and native
    Databento bar topics. See module docstring for the full rationale.

    Lifecycle:

    * ``__init__`` builds the inverse map (native → canonical) so the
      inbound handler can look up the canonical bar type in O(1).
    * :meth:`on_start` subscribes to each NATIVE bar type via
      ``self.subscribe_bars``. The data-engine routes these to the
      Databento client (which owns the native venue per ``venue_dataset_map``).
      Subscribing here is what causes Databento to actually start
      streaming.
    * :meth:`on_bar` is invoked by the actor base class whenever a bar
      arrives on a subscribed topic. The handler retags the venue to
      IBKR and republishes onto the canonical bar topic; the strategy's
      bus subscription picks it up.
    """

    def __init__(self, config: SymbologyShimActorConfig) -> None:
        super().__init__(config=config)
        # ``config`` is also stored on ``self.config`` by the base class,
        # but we keep an explicitly-typed reference so mypy can see the
        # concrete schema without an ``isinstance`` narrowing every read.
        self._shim_config: SymbologyShimActorConfig = config

        # Inverse map for the inbound path: native_bar_type_str → canonical
        # bar_type_str. Built once at construction so the handler can
        # look up the canonical topic without iterating the forward map.
        self._native_to_canonical: dict[str, str] = {
            native: canonical for canonical, native in config.canonical_to_native_bar_types.items()
        }

        # Audit sink — callers install a real sink via :meth:`set_audit_sink`
        # after construction. The default is None (no-op); the underlying
        # shim treats None as "skip audit".
        self._audit_sink: Callable[[dict[str, Any]], None] | None = None

    # -- AUDIT SINK ------------------------------------------------------

    def set_audit_sink(
        self,
        sink: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Install an audit-metadata sink for the inbound retag path.

        Per the shim's contract the sink receives a dict with
        ``provider`` / ``dataset`` / ``native_venue`` / ``native_symbol``
        / ``original_instrument_id`` / ``ts_event`` keys. Exceptions
        from the sink propagate — callers wrap as needed.

        The sink is intentionally NOT part of the persisted config
        because it's a non-serializable callable; this setter lets the
        subprocess wire its audit log writer in after the kernel has
        instantiated the actor via ``ActorFactory.create``.
        """
        self._audit_sink = sink

    # -- LIFECYCLE -------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to every NATIVE bar type at startup.

        Iterates ``canonical_to_native_bar_types.values()`` and calls
        :meth:`Actor.subscribe_bars` for each. The data-engine routes
        the subscription to the Databento client (since Databento owns
        the native venue per ``venue_dataset_map``), causing Databento
        to start streaming. Without this step the strategy's
        ``.IBKR``-tagged subscription would never trigger a Databento
        feed.

        The subscription handler installed by ``subscribe_bars`` is
        ``Actor.handle_bar`` which dispatches to :meth:`on_bar` when
        the actor is in the RUNNING state.
        """
        for native_str in self._shim_config.canonical_to_native_bar_types.values():
            self.subscribe_bars(BarType.from_str(native_str))

    # -- BAR HANDLER -----------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """Retag an incoming native bar to canonical and republish.

        Called by the base class when a bar matching one of our
        outbound subscriptions arrives on the bus. The handler:

        1. Looks up the canonical bar-type string for the incoming
           native bar (via the inverse map built at ``__init__``). If
           we receive a bar for a topic we never subscribed to, we
           drop it — that's a configuration drift the supervisor
           should catch upstream, and the actor's correctness
           invariant is "only retag what's in the configured map".
        2. Calls :func:`retag_inbound_bar` to produce a new Bar whose
           venue is ``IBKR``. The audit sink (if installed) receives
           the provenance metadata.
        3. Publishes the retagged bar onto the canonical bar topic.
           The topic format ``data.bars.{bar_type}`` mirrors the
           Nautilus data-engine's own publication path
           (verified in ``nautilus_trader/data/engine.pyx:2254``).
           Publishing on this topic is what causes the strategy's
           ``subscribe_bars`` subscription to fire.
        """
        native_bar_type_str = str(bar.bar_type)
        canonical_str = self._native_to_canonical.get(native_bar_type_str)
        if canonical_str is None:
            # Defensive: we shouldn't be subscribed to this in the
            # first place, but log + drop rather than crash the node.
            # The actor logger is available once registered.
            if self.log is not None:
                self.log.warning(
                    f"SymbologyShimActor received bar on unknown native "
                    f"topic {native_bar_type_str!r}; dropping (configuration "
                    f"drift — check canonical_to_native_bar_types).",
                )
            return

        # Codex iter 21 P2: ``retag_inbound_bar`` only rewrites the venue
        # suffix, keeping the native symbol. For aliased symbols (e.g.
        # Databento ``GOOG.XNAS`` → canonical ``GOOGL.IBKR``) that means
        # the retagged bar's ``bar_type`` would be ``GOOG.IBKR-...`` even
        # though we publish on the canonical ``GOOGL.IBKR-...`` topic.
        # Strategies that route by ``bar.bar_type`` would never see the
        # bar they subscribed to. Fix: emit audit metadata via the retag
        # helper (legacy behavior preserved) BUT construct the published
        # Bar from the configured canonical ``bar_type`` so topic and
        # payload always agree.
        retag_inbound_bar(bar, audit_metadata_sink=self._audit_sink)
        canonical_bar_type = BarType.from_str(canonical_str)
        # Codex iter 22 P2: forward ``is_revision`` so Databento
        # correction bars (later updates to a published bar's OHLCV)
        # keep their revision marker. The Bar constructor defaults
        # ``is_revision=False`` — without explicit pass-through,
        # downstream handlers would see corrections as ordinary new
        # bars and skip whatever revision-specific logic they have.
        canonical_bar = Bar(
            canonical_bar_type,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.ts_event,
            bar.ts_init,
            bool(bar.is_revision),
        )
        topic = f"data.bars.{canonical_bar_type}"
        # ``self.msgbus`` is the registered MessageBus instance set by
        # ``Actor.register_base``. ``publish`` is the public Python API
        # (``publish_c`` is Cython-only).
        self.msgbus.publish(topic=topic, msg=canonical_bar)
