"""Databento live data-client config (PR 1 T10).

Split into two halves to match the supervisor/subprocess async-vs-sync
boundary (Codex iter 4 P1):

- ``resolve_databento_targets`` (ASYNC): runs in the supervisor's payload
  factory. Has a DB session. Resolves canonical ``<SYM>.IBKR`` IDs into
  the native (dataset, symbol, venue) triples via the symbology shim.
- ``build_databento_data_client_config`` (SYNC): runs in the TradingNode
  subprocess. Has NO DB session. Assembles ``DatabentoDataClientConfig``
  from already-resolved targets that were passed through the START
  command payload.

The supervisor calls both — first resolves, then embeds the result in
the payload; the subprocess deserializes the payload and calls the
sync builder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nautilus_trader.adapters.databento.config import DatabentoDataClientConfig
from nautilus_trader.model.identifiers import InstrumentId

from msai.services.symbology_shim import resolve_for_databento

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ResolvedDatabentoTargets:
    """Already-resolved Databento subscription targets.

    Built in the supervisor's async path; consumed in the subprocess's
    sync path. Serializable to JSON via ``__dict__`` (string-valued
    fields only) because the START command payload is a JSON-able dict
    at the LiveCommandBus level.

    The companion ``venue_dataset_map`` is the AUTHORITATIVE mapping
    the shim produced — it's passed straight to ``DatabentoDataClientConfig``
    so Nautilus uses our dataset choice (Codex iter 4 P2-2), not the
    default publisher-lookup behavior.
    """

    native_instrument_ids: list[str]  # e.g. ["AAPL.XNAS", "SPY.XNAS"]
    venue_dataset_map: dict[str, str]  # e.g. {"XNAS": "EQUS.MINI"}


async def resolve_databento_targets(
    *,
    canonical_ids: list[str],
    session: AsyncSession,
    as_of_date: date,
) -> ResolvedDatabentoTargets:
    """Async: resolve canonical ``<SYM>.IBKR`` IDs → Databento native targets.

    Runs in the supervisor's payload factory (which has an AsyncSession
    + access to as-of date). The result is embedded in the START command
    payload as ``native_instrument_ids`` + ``venue_dataset_map`` so the
    subprocess can assemble the Databento client config without doing
    its own DB resolution.
    """
    native_ids: list[str] = []
    venue_to_dataset: dict[str, str] = {}
    for canonical_str in canonical_ids:
        target = await resolve_for_databento(
            InstrumentId.from_str(canonical_str),
            session=session,
            as_of_date=as_of_date,
        )
        native_ids.append(f"{target.native_symbol}.{target.native_venue}")
        # Authoritative: bind native_venue → dataset so Nautilus uses our
        # shim's choice rather than the default publisher lookup (Codex iter 4 P2-2).
        venue_to_dataset[target.native_venue] = target.dataset
    return ResolvedDatabentoTargets(
        native_instrument_ids=native_ids,
        venue_dataset_map=venue_to_dataset,
    )


def build_databento_data_client_config(
    *,
    native_instrument_ids: list[str],
    venue_dataset_map: dict[str, str],
    api_key: str,
) -> DatabentoDataClientConfig:
    """Sync: assemble ``DatabentoDataClientConfig`` from pre-resolved targets.

    Runs in the TradingNode subprocess (which has NO DB session). All
    canonical→native resolution happens upstream in
    :func:`resolve_databento_targets`.

    PR 1 pins ``reconnect_timeout_mins=10`` explicitly (council 2026-05-29)
    so a future Nautilus default change doesn't shift the safety budget.
    """
    return DatabentoDataClientConfig(
        api_key=api_key,
        instrument_ids=[InstrumentId.from_str(s) for s in native_instrument_ids],
        venue_dataset_map=venue_dataset_map,
        reconnect_timeout_mins=10,
        use_exchange_as_venue=True,
    )
