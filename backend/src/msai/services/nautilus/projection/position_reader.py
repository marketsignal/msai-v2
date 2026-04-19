"""PositionReader — snapshot reads for the live UI (Phase 3 task 3.5).

The live UI needs to know "what positions / what account does
deployment X have right now?" The answer comes from one of two
sources:

1. **Fast path** — the local :class:`ProjectionState` (populated
   by the :class:`StateApplier` from the state pub/sub channel
   in 3.4). This is in-memory, no Redis round-trip, and it's
   the steady-state read path for ANY worker that has seen at
   least one event for the deployment.

2. **Cold path** — an ephemeral Nautilus :class:`Cache` rebuilt
   per request from Redis. Used the first time a worker is
   asked about a deployment whose state hasn't been streamed to
   it yet (worker just restarted, or deployment just started
   and the StateApplier hasn't received an event yet).

After the cold path runs, its result is written back into
``ProjectionState`` via :meth:`ProjectionState.hydrate_from_cold_read`,
so the next call for the same deployment naturally lands on
the fast path. Cold reads happen AT MOST once per deployment
per worker restart.

Why ephemeral and not a long-lived ``Cache``: Nautilus's
``Cache`` is a one-shot loader, NOT a live view. ``cache_all()``
is a single batch read; the in-memory state never updates from
subsequent Redis writes. A long-lived ``Cache`` would drift
silently after the first read (Codex v3 P1).

Why per-domain hydration flags (``is_positions_hydrated`` /
``is_account_hydrated``) and not a single ``has_seen``: a
single coarse flag has two failure modes (Codex v6 P1):

- A non-state-affecting event (``FillEvent``,
  ``OrderStatusChange``) flips the flag without populating
  positions, so the fast path would return ``[]`` even when
  real positions exist in Redis.
- An event filtered before ``apply()`` never flips the flag,
  so the cold path would fire forever for that deployment.

Per-domain flags fix both: hydration is tracked separately for
positions vs. accounts, and the flag is set ONLY when the
corresponding domain is actually populated.

Why only-if-still-cold hydration (Codex v7 P1): the
``hydrate_from_cold_read`` call is a no-op for any domain that
was hydrated between the caller's fast-path check and the
hydrate call. If the StateApplier raced us, we leave its
fresher data alone. ``PositionReader`` returns the CURRENT
state value (not the cold-read result) so the caller sees the
fresher data in the race case.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import msgspec
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.cache.database import (  # type: ignore[import-not-found]
    CacheDatabaseAdapter,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.identifiers import (  # type: ignore[import-not-found]
    AccountId,
    TraderId,
)
from nautilus_trader.serialization.serializer import (  # type: ignore[import-not-found]
    MsgSpecSerializer,
)

from msai.services.nautilus.live_node_config import build_redis_database_config
from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    PositionSnapshot,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from msai.services.nautilus.projection.projection_state import ProjectionState


def _money_to_decimal(money: Any) -> Decimal:
    """``Money.__str__`` returns ``"0.00 USD"`` which ``Decimal``
    rejects. Extract the numeric portion via the canonical
    ``Money.as_decimal()`` when available, or strip the currency
    suffix as a fallback. Returns 0 for None input."""
    if money is None:
        return Decimal(0)
    as_decimal = getattr(money, "as_decimal", None)
    if callable(as_decimal):
        return Decimal(as_decimal())
    text = str(money).split(" ", 1)[0]
    return Decimal(text) if text else Decimal(0)


log = logging.getLogger(__name__)


class PositionReader:
    """Snapshot reads of positions/accounts for the live UI.

    Read flow (v8, Codex v7 P1 fix):

    1. Check ``is_positions_hydrated`` / ``is_account_hydrated``
       on the local ProjectionState.
       - True: state has been written (by StateApplier or by a
         previous cold read). Return ``state.positions(...)`` /
         ``state.account(...)`` verbatim. An empty list / None
         result is authoritative.
       - False: the cold path runs.
    2. Cold path: build an ephemeral CacheDatabaseAdapter +
       Cache, call ``cache_all()``, read, dispose.
    3. THEN call ``state.hydrate_from_cold_read(...)`` with the
       result. The hydrate is only-if-still-cold (Codex v7 P1).
    4. Return the CURRENT state value (not the cold-read
       result). In the race case, this returns the fresher
       pub/sub data instead of our stale cold-read data.

    NEVER keeps a long-lived Cache. The Cache is a one-shot
    loader, not a live view (Codex v3 P1).
    """

    def __init__(self, projection_state: ProjectionState) -> None:
        """The cold-path Redis connection details come from the
        shared :func:`build_redis_database_config` helper, NOT
        from constructor arguments. This guarantees the cold
        reader uses the SAME ``DatabaseConfig`` (host, port,
        username, password, ssl) the live trading subprocess
        wrote with — Codex batch 8 P1 fix. Building a separate
        ``DatabaseConfig`` here would silently drop credentials
        and TLS on auth-protected Redis.
        """
        self._state = projection_state
        self._cache_config = CacheConfig(
            database=build_redis_database_config(),
            encoding="msgpack",
        )

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    async def get_open_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        """Return the open positions for one deployment as a
        list of :class:`PositionSnapshot`. Fast path if state
        is hydrated; cold path otherwise."""
        if self._state.is_positions_hydrated(deployment_id):
            return self._state.positions(deployment_id)

        positions = self._read_via_ephemeral_cache_positions(
            deployment_id, trader_id, strategy_id_full
        )
        # Only-if-still-cold hydrate. If the StateApplier raced
        # us, this is a no-op and the next call returns the
        # fresher pub/sub data.
        self._state.hydrate_from_cold_read(deployment_id, positions=positions)
        # Return CURRENT state, not the cold-read result.
        return self._state.positions(deployment_id)

    async def get_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        """Return the account snapshot for one deployment.
        Fast path if state is hydrated; cold path otherwise."""
        if self._state.is_account_hydrated(deployment_id):
            return self._state.account(deployment_id)

        account = self._read_via_ephemeral_cache_account(deployment_id, trader_id, account_id)
        # Always hydrate — even when the cold read returned
        # ``None``. ``None`` is a valid hydrated value ("the
        # cold read found no account") and writing it flips
        # ``is_account_hydrated`` to True so the next call
        # serves ``None`` from the fast path instead of
        # cold-reading again. Codex batch 8 P1 fix.
        self._state.hydrate_from_cold_read(deployment_id, account=account)
        return self._state.account(deployment_id)

    # ------------------------------------------------------------------
    # Cold-path adapter construction
    # ------------------------------------------------------------------

    def _build_adapter(self, trader_id: str) -> CacheDatabaseAdapter:
        """Construct a fresh ``CacheDatabaseAdapter`` with the
        verified Nautilus 1.223.0 signature.

        - ``trader_id``: from the ``live_deployments`` row
          (deterministic ``MSAI-{slug}``, decision #7).
        - ``instance_id``: fresh ``UUID4`` per request — this
          is the adapter-instance ID, NOT the trader id, and
          must be unique per call.
        - ``serializer``: ``MsgSpecSerializer`` constructed the
          same way ``nautilus_trader/system/kernel.py:313-317``
          constructs it. The ``encoding`` parameter is the
          msgspec MODULE (``msgspec.msgpack``), NOT the string
          ``"msgpack"``. Passing a string would raise on first
          encode. ``timestamps_as_str=True`` matches the
          subprocess's serializer config (kernel.py:315
          ``# Hardcoded for now``) so reads decode correctly.
        - ``config``: the same ``CacheConfig`` the live
          subprocess uses, so the adapter reads from the same
          Redis keyspace.
        """
        # Nautilus's UUID4() takes no arguments — it generates a
        # fresh v4 UUID internally (uuid.pyx:39 ``self._mem =
        # uuid4_new()``). The plan's UUID4(uuid.uuid4().hex) was
        # wrong — fixed during implementation.
        return CacheDatabaseAdapter(
            trader_id=TraderId(trader_id),
            instance_id=UUID4(),
            serializer=MsgSpecSerializer(
                encoding=msgspec.msgpack,
                timestamps_as_str=True,
                timestamps_as_iso8601=False,
            ),
            config=self._cache_config,
        )

    # ------------------------------------------------------------------
    # Cold-path reads
    # ------------------------------------------------------------------

    def _read_via_ephemeral_cache_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        """Read open positions for a strategy directly from the
        ``CacheDatabaseAdapter``. Per-request — the adapter is
        disposed at the end.

        Why the adapter directly and not ``Cache.cache_all() +
        cache.positions_open(...)``: the ``Cache`` wrapper's
        ``cache_positions``/``cache_orders`` paths silently load
        zero rows in our Nautilus version even when the adapter's
        ``load_orders()``/``load_positions()`` return the rows
        correctly. Surfaced end-to-end 2026-04-16 during Bug B
        investigation: ``redis-cli MONITOR`` showed the adapter
        issuing the right SCAN + LRANGE commands against
        ``trader-{trader}:positions:*`` and ``:orders:*``, and
        deserializing the events successfully, but the Cache's
        internal position map stayed empty. Going direct to the
        adapter bypasses the wrapper entirely — there's no state
        we need from ``Cache`` beyond the loaded ``Position``
        objects themselves.
        """
        adapter = self._build_adapter(trader_id)
        try:
            positions_by_id = adapter.load_positions()
            raw = [
                position
                for position in positions_by_id.values()
                if str(position.strategy_id) == strategy_id_full and position.is_open
            ]
            return [self._to_snapshot(p, deployment_id) for p in raw]
        finally:
            adapter.close()

    def _read_via_ephemeral_cache_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        # Nautilus ``AccountId`` requires the canonical
        # ``"VENUE-ACCOUNT"`` form (e.g., ``"INTERACTIVE_BROKERS-DUP733213"``)
        # — passing the bare broker account string (``"DUP733213"``) raises
        # ``ValueError: value was malformed: did not contain a hyphen '-'``
        # and takes down the whole WS snapshot before any downstream field
        # is emitted. Surfaced end-to-end 2026-04-16 during Phase 2 #4
        # reconnect-snapshot verification: a fresh backend with an empty
        # ``ProjectionState`` fell to this cold path on every connect and
        # every client got a 1011 close. Qualify with the IB venue prefix
        # here so the format always matches what Nautilus's own
        # ``AccountState`` events emit.
        qualified_account = account_id if "-" in account_id else f"INTERACTIVE_BROKERS-{account_id}"
        adapter = self._build_adapter(trader_id)
        try:
            # Go direct to the adapter — see the docstring on
            # ``_read_via_ephemeral_cache_positions`` for why the
            # ``Cache`` wrapper is bypassed (Bug B, 2026-04-16).
            account = adapter.load_account(AccountId(qualified_account))
            if account is None:
                return None
            return self._to_account_update(account, deployment_id)
        finally:
            adapter.close()

    # ------------------------------------------------------------------
    # Nautilus → internal model adapters
    # ------------------------------------------------------------------

    @staticmethod
    def _to_snapshot(position: Any, deployment_id: UUID) -> PositionSnapshot:
        """Convert a Nautilus ``Position`` cached object to our
        internal ``PositionSnapshot``. Done as a staticmethod
        so the test surface stays narrow — no DB / Redis
        dependencies.

        Nautilus ``Position`` returns ``Money`` for ``realized_pnl``
        (str form is ``"0.00 USD"``, which ``Decimal`` cannot parse).
        ``unrealized_pnl`` is a *method* that takes a last-quote
        argument — we don't have the live quote in this read path,
        so we surface 0 (the dashboard can compute it client-side
        from the position + a separate quote feed if needed). Bug B
        surfaced this when switching the reader from the broken
        ``Cache`` wrapper to the working adapter path — the wrapper
        never returned any Position, so this ``Money.__str__``
        mismatch had gone undetected.
        """
        ts = PositionReader._coerce_ts(getattr(position, "ts_last", None))
        return PositionSnapshot(
            deployment_id=deployment_id,
            instrument_id=str(position.instrument_id),
            qty=Decimal(str(getattr(position, "quantity", "0"))),
            avg_price=Decimal(str(getattr(position, "avg_px_open", "0"))),
            unrealized_pnl=Decimal(0),
            realized_pnl=_money_to_decimal(getattr(position, "realized_pnl", None)),
            ts=ts,
        )

    @staticmethod
    def _to_account_update(account: Any, deployment_id: UUID) -> AccountStateUpdate:
        """Convert a Nautilus ``Account`` cached object to our
        internal ``AccountStateUpdate``."""
        balance = Decimal("0")
        margin_used = Decimal("0")
        margin_available = Decimal("0")
        try:
            balances = account.balances() if callable(account.balances) else account.balances
            if balances:
                first = next(iter(balances.values())) if isinstance(balances, dict) else balances[0]
                # Nautilus ``AccountBalance`` fields are ``Money`` —
                # same "0.00 USD" parsing issue as ``realized_pnl``
                # in ``_to_snapshot``. Use the shared helper so
                # ``Money.as_decimal()`` is called when available,
                # otherwise the currency suffix is stripped.
                total = getattr(first, "total", None)
                balance = _money_to_decimal(total() if callable(total) else total)
                locked = getattr(first, "locked", None)
                margin_used = _money_to_decimal(locked() if callable(locked) else locked)
                free = getattr(first, "free", None)
                margin_available = _money_to_decimal(free() if callable(free) else free)
        except Exception:  # noqa: BLE001
            log.exception(
                "position_reader_account_balance_extract_failed",
                extra={"deployment_id": str(deployment_id)},
            )
        return AccountStateUpdate(
            deployment_id=deployment_id,
            account_id=str(account.id),
            balance=balance,
            margin_used=margin_used,
            margin_available=margin_available,
            ts=PositionReader._coerce_ts(getattr(account, "ts_last", None)),
        )

    @staticmethod
    def _coerce_ts(ts_ns: int | None) -> datetime:
        """Nautilus timestamps are int64 nanoseconds since
        epoch. Convert to UTC ``datetime``; fall back to
        ``now()`` if missing."""
        from datetime import UTC, datetime

        if ts_ns is None:
            return datetime.now(UTC)
        return datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=UTC)
