from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import msgspec
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    get_cached_ib_client,
    get_cached_interactive_brokers_instrument_provider,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.model.instruments import Instrument
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.config import settings
from msai.models import InstrumentDefinition
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.instruments import instrument_from_payload, instrument_to_payload


@dataclass(frozen=True, slots=True)
class ResolvedInstrumentDefinition:
    instrument_id: str
    raw_symbol: str
    venue: str
    instrument_type: str
    security_type: str | None
    asset_class: str
    instrument_data: dict[str, Any]
    contract_details: dict[str, Any] | None
    provider: str = "interactive_brokers"

    @classmethod
    def from_model(cls, model: InstrumentDefinition) -> ResolvedInstrumentDefinition:
        return cls(
            instrument_id=model.instrument_id,
            raw_symbol=model.raw_symbol,
            venue=model.venue,
            instrument_type=model.instrument_type,
            security_type=model.security_type,
            asset_class=model.asset_class,
            instrument_data=dict(model.instrument_data),
            contract_details=dict(model.contract_details) if model.contract_details is not None else None,
            provider=model.provider,
        )

    def to_instrument(self) -> Instrument:
        return instrument_from_payload(self.instrument_data)


class NautilusInstrumentService:
    def __init__(self, *, databento: DatabentoClient | None = None) -> None:
        self._clock = LiveClock()
        self._cache = Cache()
        self._message_bus = MessageBus(TraderId(settings.nautilus_trader_id), self._clock)
        self._databento = databento or DatabentoClient()

    async def ensure_definitions(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
        *,
        paper_trading: bool,
        force_refresh: bool = False,
    ) -> list[ResolvedInstrumentDefinition]:
        definitions: list[ResolvedInstrumentDefinition] = []
        seen: set[str] = set()

        for requested in requested_instruments:
            canonical_id = await self._canonical_instrument_id(session, requested)
            if canonical_id in seen:
                continue
            seen.add(canonical_id)

            model = await session.get(InstrumentDefinition, canonical_id)
            if model is not None and not force_refresh:
                definitions.append(ResolvedInstrumentDefinition.from_model(model))
                continue

            resolved = await self._resolve_with_nautilus(canonical_id, paper_trading=paper_trading)
            if model is None:
                model = InstrumentDefinition(instrument_id=resolved.instrument_id)
                session.add(model)

            model.provider = resolved.provider
            model.raw_symbol = resolved.raw_symbol
            model.venue = resolved.venue
            model.instrument_type = resolved.instrument_type
            model.security_type = resolved.security_type
            model.asset_class = resolved.asset_class
            model.instrument_data = resolved.instrument_data
            model.contract_details = resolved.contract_details
            definitions.append(resolved)

        await session.flush()
        return definitions

    async def canonicalize_instruments(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
        *,
        paper_trading: bool,
        force_refresh: bool = False,
    ) -> list[str]:
        definitions = await self.ensure_definitions(
            session,
            requested_instruments,
            paper_trading=paper_trading,
            force_refresh=force_refresh,
        )
        return [definition.instrument_id for definition in definitions]

    async def canonicalize_live_instruments(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
    ) -> list[str]:
        canonical_ids: list[str] = []
        seen: set[str] = set()

        for requested in requested_instruments:
            canonical_id = await self._canonical_instrument_id(session, requested)
            if canonical_id in seen:
                continue
            seen.add(canonical_id)
            canonical_ids.append(canonical_id)

        return canonical_ids

    async def ensure_backtest_definitions(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
        *,
        force_refresh: bool = False,
    ) -> list[ResolvedInstrumentDefinition]:
        definitions: list[ResolvedInstrumentDefinition] = []
        seen: set[str] = set()

        for requested in requested_instruments:
            canonical_id = await self._canonical_backtest_instrument_id(session, requested)
            if canonical_id in seen:
                continue
            seen.add(canonical_id)

            model = await session.get(InstrumentDefinition, canonical_id)
            if model is None:
                raise ValueError(
                    "Backtests require persisted Databento instrument definitions. "
                    "Run market-data ingest for the requested symbols before backtesting."
                )
            if model.provider != "databento" and not force_refresh:
                preferred = await self._latest_definition_by_raw_symbol(
                    session,
                    model.raw_symbol,
                    provider="databento",
                )
                if preferred is not None:
                    definitions.append(ResolvedInstrumentDefinition.from_model(preferred))
                    continue

            definitions.append(ResolvedInstrumentDefinition.from_model(model))

        return definitions

    async def canonicalize_backtest_instruments(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
        *,
        force_refresh: bool = False,
    ) -> list[str]:
        definitions = await self.ensure_backtest_definitions(
            session,
            requested_instruments,
            force_refresh=force_refresh,
        )
        return [definition.instrument_id for definition in definitions]

    async def ingest_databento_definitions(
        self,
        session: AsyncSession,
        requested_instruments: list[str],
        *,
        dataset: str,
        start: str,
        end: str,
        force_refresh: bool = False,
    ) -> list[ResolvedInstrumentDefinition]:
        definitions: list[ResolvedInstrumentDefinition] = []
        seen_raw_symbols: set[str] = set()

        for requested in requested_instruments:
            raw_symbol = _raw_symbol_from_request(requested)
            if raw_symbol in seen_raw_symbols:
                continue
            seen_raw_symbols.add(raw_symbol)

            cached = await self._latest_definition_by_raw_symbol(
                session,
                raw_symbol,
                provider="databento",
            )
            fetch_start = start
            fetch_end = end
            if cached is not None and not force_refresh:
                if _continuous_definition_needs_refresh(
                    model=cached,
                    requested_symbol=raw_symbol,
                    start=start,
                    end=end,
                ):
                    cached_start, cached_end = _definition_window_bounds(cached)
                    if cached_start is not None and cached_end is not None:
                        fetch_start = min(cached_start, start)
                        fetch_end = max(cached_end, end)
                else:
                    definitions.append(ResolvedInstrumentDefinition.from_model(cached))
                    continue
            elif cached is not None and force_refresh:
                cached_start, cached_end = _definition_window_bounds(cached)
                if cached_start is not None and cached_end is not None:
                    fetch_start = min(cached_start, start)
                    fetch_end = max(cached_end, end)

            definition_path = _definition_file_path(
                dataset=dataset,
                raw_symbol=raw_symbol,
                start=fetch_start,
                end=fetch_end,
            )
            instruments = await self._databento.fetch_definition_instruments(
                raw_symbol,
                fetch_start,
                fetch_end,
                dataset=dataset,
                target_path=definition_path,
            )
            resolved = _resolved_databento_definition(
                raw_symbol=raw_symbol,
                instruments=instruments,
                dataset=dataset,
                start=fetch_start,
                end=fetch_end,
                definition_path=definition_path,
            )

            model = await session.get(InstrumentDefinition, resolved.instrument_id)
            if model is None:
                model = InstrumentDefinition(instrument_id=resolved.instrument_id)
                session.add(model)

            model.provider = resolved.provider
            model.raw_symbol = resolved.raw_symbol
            model.venue = resolved.venue
            model.instrument_type = resolved.instrument_type
            model.security_type = resolved.security_type
            model.asset_class = resolved.asset_class
            model.instrument_data = resolved.instrument_data
            model.contract_details = resolved.contract_details
            definitions.append(resolved)

        await session.flush()
        return definitions

    async def _canonical_instrument_id(self, session: AsyncSession, requested: str) -> str:
        value = requested.strip()
        if not value:
            raise ValueError("Instrument ID cannot be empty")
        if "." in value:
            cached = await session.get(InstrumentDefinition, str(InstrumentId.from_str(value)))
            if cached is not None and cached.provider == "interactive_brokers":
                return cached.instrument_id

        raw_symbol = _raw_symbol_from_request(value)
        rows = await self._instrument_ids_for_raw_symbol(
            session,
            raw_symbol,
            provider="interactive_brokers",
        )
        if len(rows) == 1:
            return str(rows[0])
        if len(rows) > 1:
            raise ValueError(
                f"Instrument symbol {raw_symbol!r} is ambiguous; provide a venue-qualified Nautilus ID"
            )

        if "." in value:
            return str(InstrumentId.from_str(value))
        return str(InstrumentId.from_str(f"{raw_symbol}.XNAS"))

    async def _canonical_backtest_instrument_id(self, session: AsyncSession, requested: str) -> str:
        value = requested.strip()
        if not value:
            raise ValueError("Instrument ID cannot be empty")
        if "." in value:
            explicit_id = str(InstrumentId.from_str(value))
            cached = await session.get(InstrumentDefinition, explicit_id)
            if cached is not None and cached.provider == "databento":
                return cached.instrument_id

        raw_symbol = _raw_symbol_from_request(value)
        rows = await self._instrument_ids_for_raw_symbol(
            session,
            raw_symbol,
            provider="databento",
        )
        if len(rows) == 1:
            return str(rows[0])
        if len(rows) > 1:
            raise ValueError(
                f"Backtest symbol {raw_symbol!r} maps to multiple persisted Databento instruments; "
                "provide the exact Databento Nautilus instrument ID."
            )

        raise ValueError(
            "Backtests require persisted Databento instrument definitions. "
            f"Run market-data ingest for {raw_symbol!r} before backtesting."
        )

    async def _latest_definition_by_raw_symbol(
        self,
        session: AsyncSession,
        raw_symbol: str,
        *,
        provider: str,
    ) -> InstrumentDefinition | None:
        return (
            await session.execute(
                select(InstrumentDefinition)
                .where(
                    InstrumentDefinition.raw_symbol == raw_symbol,
                    InstrumentDefinition.provider == provider,
                )
                .order_by(InstrumentDefinition.updated_at.desc())
            )
        ).scalars().first()

    async def _instrument_ids_for_raw_symbol(
        self,
        session: AsyncSession,
        raw_symbol: str,
        *,
        provider: str,
    ) -> list[str]:
        return (
            await session.execute(
                select(InstrumentDefinition.instrument_id)
                .where(
                    InstrumentDefinition.raw_symbol == raw_symbol,
                    InstrumentDefinition.provider == provider,
                )
                .order_by(InstrumentDefinition.updated_at.desc())
            )
        ).scalars().all()

    async def _resolve_with_nautilus(
        self,
        instrument_id: str,
        *,
        paper_trading: bool,
    ) -> ResolvedInstrumentDefinition:
        parsed_id = InstrumentId.from_str(instrument_id)
        provider = self._instrument_provider(paper_trading=paper_trading)
        client = provider._client

        await client.wait_until_ready(timeout=settings.ib_request_timeout_seconds)

        loaded_ids = await provider.load_with_return_async(parsed_id)
        if not loaded_ids:
            raise ValueError(
                f"Nautilus Interactive Brokers provider could not resolve {instrument_id}"
            )

        resolved_id = loaded_ids[0]
        instrument = provider.find(resolved_id)
        if instrument is None:
            raise ValueError(f"Nautilus provider loaded {resolved_id} without an instrument payload")

        details = await provider.instrument_id_to_ib_contract_details(resolved_id)
        instrument_data = instrument_to_payload(instrument)
        security_type = None
        if details is not None and details.contract is not None:
            security_type = details.contract.secType or None

        return ResolvedInstrumentDefinition(
            instrument_id=str(instrument.id),
            raw_symbol=instrument.raw_symbol.value,
            venue=instrument.id.venue.value,
            instrument_type=str(instrument_data.get("type", type(instrument).__name__)),
            security_type=security_type,
            asset_class=_asset_class_for_security_type(security_type),
            instrument_data=instrument_data,
            contract_details=msgspec.to_builtins(details) if details is not None else None,
        )

    def _instrument_provider(self, *, paper_trading: bool):
        loop = asyncio.get_running_loop()
        port = settings.ib_gateway_port_paper if paper_trading else settings.ib_gateway_port_live
        client = get_cached_ib_client(
            loop=loop,
            msgbus=self._message_bus,
            cache=self._cache,
            clock=self._clock,
            host=settings.ib_gateway_host,
            port=port,
            client_id=settings.ib_instrument_client_id,
            request_timeout_secs=settings.ib_request_timeout_seconds,
        )
        config = InteractiveBrokersInstrumentProviderConfig(
            symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        )
        return get_cached_interactive_brokers_instrument_provider(client, self._clock, config)


def _asset_class_for_security_type(security_type: str | None) -> str:
    if security_type in {"FUT", "CONTFUT"}:
        return "futures"
    if security_type in {"OPT", "FOP"}:
        return "options"
    if security_type == "CASH":
        return "fx"
    if security_type in {"CRYPTO", "CRYPTOCURRENCY"}:
        return "crypto"
    return "stocks"


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _raw_symbol_from_request(requested: str) -> str:
    value = requested.strip()
    if not value:
        raise ValueError("Instrument ID cannot be empty")
    if _DATABENTO_CONTINUOUS_SYMBOL.match(value):
        return value
    if "." in value:
        return InstrumentId.from_str(value).symbol.value
    return value


def _definition_file_path(*, dataset: str, raw_symbol: str, start: str, end: str) -> Path:
    safe_dataset = dataset.replace("/", "_")
    safe_start = start.replace(":", "-")
    safe_end = end.replace(":", "-")
    return (
        settings.databento_definition_root
        / safe_dataset
        / raw_symbol
        / f"{safe_start}_{safe_end}.definition.dbn.zst"
    )


def _resolved_databento_definition(
    *,
    raw_symbol: str,
    instruments: list[Instrument],
    dataset: str,
    start: str,
    end: str,
    definition_path: Path,
) -> ResolvedInstrumentDefinition:
    matching = [instrument for instrument in instruments if instrument.raw_symbol.value == raw_symbol]
    if not matching and _DATABENTO_CONTINUOUS_SYMBOL.match(raw_symbol):
        matching = instruments
    if not matching:
        raise ValueError(
            f"Databento definition data for {raw_symbol!r} did not decode into a Nautilus instrument"
        )

    selected = max(
        matching,
        key=lambda instrument: str(
            instrument_to_payload(instrument).get("ts_init")
            or instrument_to_payload(instrument).get("ts_event")
            or ""
        ),
    )
    instrument_data = instrument_to_payload(selected)
    requested_symbol = raw_symbol if _DATABENTO_CONTINUOUS_SYMBOL.match(raw_symbol) else None
    if requested_symbol is not None:
        synthetic_instrument_id = f"{requested_symbol}.{selected.id.venue.value}"
        instrument_data["id"] = synthetic_instrument_id
        instrument_data["raw_symbol"] = requested_symbol
        requested_start_ns = _iso_start_ns(start)
        requested_end_ns = _iso_end_ns(end)
        activation_values = [
            int(value)
            for instrument in matching
            if (value := instrument_to_payload(instrument).get("activation_ns")) is not None
        ]
        expiration_values = [
            int(value)
            for instrument in matching
            if (value := instrument_to_payload(instrument).get("expiration_ns")) is not None
        ]
        if activation_values:
            instrument_data["activation_ns"] = min(min(activation_values), requested_start_ns)
        else:
            instrument_data["activation_ns"] = requested_start_ns
        if expiration_values:
            instrument_data["expiration_ns"] = max(max(expiration_values), requested_end_ns)
        else:
            instrument_data["expiration_ns"] = requested_end_ns
    instrument_type = str(instrument_data.get("type", type(selected).__name__))
    security_type = _security_type_for_instrument_type(instrument_type)

    return ResolvedInstrumentDefinition(
        instrument_id=instrument_data["id"],
        raw_symbol=str(instrument_data["raw_symbol"]),
        venue=selected.id.venue.value,
        instrument_type=instrument_type,
        security_type=security_type,
        asset_class=_asset_class_for_instrument_type(instrument_type),
        instrument_data=instrument_data,
        contract_details={
            "dataset": dataset,
            "schema": "definition",
            "definition_start": start,
            "definition_end": end,
            "definition_file_path": str(definition_path),
            "requested_symbol": raw_symbol,
            "underlying_instrument_id": str(selected.id),
            "underlying_raw_symbol": selected.raw_symbol.value,
        },
        provider="databento",
    )


def _asset_class_for_instrument_type(instrument_type: str) -> str:
    if instrument_type in {"FuturesContract", "FuturesSpread"}:
        return "futures"
    if instrument_type in {"OptionContract", "OptionSpread"}:
        return "options"
    if instrument_type in {"CurrencyPair"}:
        return "fx"
    if instrument_type in {"CryptoFuture", "CryptoOption", "CryptoPerpetual", "PerpetualContract"}:
        return "crypto"
    return "stocks"


def _security_type_for_instrument_type(instrument_type: str) -> str | None:
    if instrument_type in {"FuturesContract", "FuturesSpread"}:
        return "FUT"
    if instrument_type in {"OptionContract", "OptionSpread"}:
        return "OPT"
    if instrument_type == "CurrencyPair":
        return "CASH"
    if instrument_type in {"CryptoFuture", "CryptoOption", "CryptoPerpetual", "PerpetualContract"}:
        return "CRYPTO"
    if instrument_type == "Equity":
        return "STK"
    return None


instrument_service = NautilusInstrumentService()


def _definition_window_bounds(model: InstrumentDefinition) -> tuple[str | None, str | None]:
    details = model.contract_details if isinstance(model.contract_details, dict) else None
    if details is None:
        return (None, None)
    start = details.get("definition_start")
    end = details.get("definition_end")
    if not isinstance(start, str) or not isinstance(end, str):
        return (None, None)
    return (start, end)


def _continuous_definition_needs_refresh(
    *,
    model: InstrumentDefinition,
    requested_symbol: str,
    start: str,
    end: str,
) -> bool:
    if not _DATABENTO_CONTINUOUS_SYMBOL.match(requested_symbol):
        return False
    cached_start, cached_end = _definition_window_bounds(model)
    if cached_start is None or cached_end is None:
        return True
    return start < cached_start or end > cached_end


def _iso_start_ns(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1_000_000_000)


def _iso_end_ns(value: str) -> int:
    return int(
        (datetime.fromisoformat(value).replace(tzinfo=UTC) + timedelta(days=1, microseconds=-1)).timestamp()
        * 1_000_000_000
    )
