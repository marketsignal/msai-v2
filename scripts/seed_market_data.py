"""Generate synthetic OHLCV minute bar data for E2E testing.

Creates deterministic synthetic bars at 09:30–15:59 UTC on January 2025
weekdays. These are NOT exchange sessions or evidence of market completeness.

Writes Parquet files to: {data_root}/parquet/stocks/{SYMBOL}/2025/01.parquet

Usage:
    python scripts/seed_market_data.py data
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition
    from msai.services.nautilus.security_master.service import SecurityMaster
    from msai.services.nautilus.security_master.types import Provider

SYMBOLS: dict[str, dict[str, float]] = {
    "AAPL": {"start_price": 243.0, "volatility": 0.0008, "drift": 0.00001},
    "MSFT": {"start_price": 420.0, "volatility": 0.0007, "drift": 0.000008},
    "SPY": {"start_price": 595.0, "volatility": 0.0004, "drift": 0.000005},
}

FIXTURE_ID = "msai-nautilus-v2-research-january-2025-v1"
MANIFEST_NAME = "research-fixture-manifest.json"
REGISTRY_NAME = "research-fixture-registry.json"

TRADING_HOURS_START = 9 * 60 + 30  # 09:30 UTC; deliberately not an ET session
TRADING_HOURS_END = 16 * 60  # 16:00 UTC, exclusive
MINUTES_PER_DAY = TRADING_HOURS_END - TRADING_HOURS_START  # 390


def generate_trading_days(year: int, month: int) -> list[datetime]:
    """Return synthetic UTC weekdays; no holiday/calendar claim."""
    days = []
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)

    current = start
    while current < end:
        if current.weekday() < 5:  # Mon-Fri
            days.append(current)
        current += timedelta(days=1)
    return days


def generate_bars(
    symbol: str, params: dict[str, float], year: int = 2025, month: int = 1,
) -> pd.DataFrame:
    """Generate realistic 1-minute OHLCV bars using geometric Brownian motion."""
    seed = int.from_bytes(hashlib.sha256(f"{FIXTURE_ID}:{symbol}".encode()).digest()[:8])
    rng = np.random.default_rng(seed)
    trading_days = generate_trading_days(year, month)

    timestamps = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []

    price = params["start_price"]
    vol = params["volatility"]
    drift = params["drift"]

    for day in trading_days:
        base_volume = rng.integers(50_000, 200_000)

        for minute_offset in range(MINUTES_PER_DAY):
            hour = (TRADING_HOURS_START + minute_offset) // 60
            minute = (TRADING_HOURS_START + minute_offset) % 60
            ts = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            timestamps.append(ts)

            # Geometric Brownian motion for price
            open_price = price
            returns = rng.normal(drift, vol, size=4)
            intra_prices = open_price * np.cumprod(1 + returns)

            close_price = float(intra_prices[-1])
            high_price = float(max(open_price, np.max(intra_prices)))
            low_price = float(min(open_price, np.min(intra_prices)))

            # Volume: higher at open/close, lower midday
            hour_factor = 1.0
            if minute_offset < 30 or minute_offset > 360:
                hour_factor = 2.5  # Opening/closing surge
            elif minute_offset < 60:
                hour_factor = 1.5
            bar_volume = int(base_volume * hour_factor * rng.uniform(0.5, 1.5))

            opens.append(round(open_price, 2))
            highs.append(round(high_price, 2))
            lows.append(round(low_price, 2))
            closes.append(round(close_price, 2))
            volumes.append(bar_volume)

            price = close_price

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, utc=True),
        "symbol": symbol,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


def write_parquet(df: pd.DataFrame, data_root: Path, asset_class: str, symbol: str) -> Path:
    """Write a DataFrame as a Parquet file in the MSAI directory structure."""
    ts = df["timestamp"].iloc[0]
    year = f"{ts.year:04d}"
    month = f"{ts.month:02d}"

    target = data_root / "parquet" / asset_class / symbol / year / f"{month}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, str(target), compression="zstd")

    return target


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def prepare_fixture(
    symbols: tuple[str, ...] = ("AAPL",),
) -> tuple[dict[str, Any], dict[str, bytes]]:
    files = {}
    records = []
    for symbol in sorted(symbols):
        frame = generate_bars(symbol, SYMBOLS[symbol])
        sink = pa.BufferOutputStream()
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), sink, compression="zstd")
        content = sink.getvalue().to_pybytes()
        relative = f"parquet/stocks/{symbol}/2025/01.parquet"
        files[relative] = content
        records.append({
            "path": relative, "sha256": hashlib.sha256(content).hexdigest(),
            "bars": len(frame), "first_timestamp": frame.timestamp.iloc[0].isoformat(),
            "last_timestamp": frame.timestamp.iloc[-1].isoformat(),
        })
    manifest = {
        "fixture_id": FIXTURE_ID, "origin": "synthetic",
        "calendar": "UTC weekdays, not exchange sessions", "year": 2025, "month": 1,
        "assumptions": "Generated prices/volume; no realistic costs or alpha validation",
        "generator": "sha256 fixture identity and symbol; numpy default_rng",
        "symbols": {symbol: SYMBOLS[symbol] for symbol in sorted(symbols)}, "files": records,
    }
    return manifest, files


def check_fixture_files(data_root: Path, manifest: dict[str, Any]) -> None:
    existing_path = data_root / MANIFEST_NAME
    if any(parent.is_symlink() for parent in (data_root, *data_root.parents)):
        raise ValueError("unmarked/symlink data root refused")
    if existing_path.is_symlink() or (data_root / REGISTRY_NAME).is_symlink():
        raise ValueError("unmarked/symlink manifest refused")
    existing = json.loads(existing_path.read_text()) if existing_path.exists() else None
    if existing is not None and existing != manifest:
        raise ValueError("conflicting fixture manifest; preserve the existing data")
    for record in manifest["files"]:
        target = data_root / record["path"]
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError("unmarked/symlink data target refused")
        if target.exists():
            if existing is None:
                raise ValueError("unmarked existing Parquet refused; use fresh isolated data root")
            if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("fixture data hash mismatch; preserve changed data")


def seed_fixture(data_root: Path, symbols: tuple[str, ...] = ("AAPL",)) -> dict[str, Any]:
    manifest, files = prepare_fixture(symbols)
    check_fixture_files(data_root, manifest)
    for relative, content in files.items():
        target = data_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
    (data_root / MANIFEST_NAME).write_bytes(canonical_bytes(manifest))
    return manifest


def validate_registry_target(environment: str, database_url: str, isolated: bool) -> None:
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
        safe = (
            isolated and environment == "development" and url.drivername == "postgresql+asyncpg"
            and url.database == "msai_v2_research"
            and url.host in {"postgres", "localhost", "127.0.0.1"}
        )
    except Exception:
        safe = False
    if not safe:
        raise ValueError(
            "registry bootstrap requires explicit isolated development target msai_v2_research",
        )


def make_security_master(db: AsyncSession) -> SecurityMaster:
    from msai.services.nautilus.security_master.service import SecurityMaster

    return SecurityMaster(db=db, qualifier=None, databento_client=None)


def definition_metadata(definition: InstrumentDefinition) -> dict[str, Any]:
    fields = (
        "raw_symbol", "listing_venue", "routing_venue", "asset_class", "provider",
        "lifecycle_state", "trading_hours", "roll_policy", "continuous_pattern",
        "hidden_from_inventory",
    )
    return {field: getattr(definition, field) for field in fields}


def alias_metadata(alias: InstrumentAlias) -> dict[str, Any]:
    return {
        "alias_string": alias.alias_string, "provider": alias.provider,
        "venue_format": alias.venue_format, "source_venue_raw": alias.source_venue_raw,
        "effective_from": alias.effective_from.isoformat(),
        "effective_to": alias.effective_to.isoformat() if alias.effective_to else None,
    }


async def bootstrap_registry(
    db: AsyncSession, manifest: dict[str, Any], binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preflight + existing owned writer inside the caller's transaction; no clients."""
    from sqlalchemy import or_, select, text

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition
    from msai.services.nautilus.security_master.service import compute_advisory_lock_key

    if "AAPL" not in manifest["symbols"]:
        raise ValueError("registry bootstrap requires the AAPL fixture")
    providers: tuple[Provider, ...] = ("databento", "interactive_brokers")
    for provider in providers:
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {
            "k": compute_advisory_lock_key(provider, "AAPL", "equity"),
        })

    async def rows() -> tuple[Sequence[InstrumentDefinition], Sequence[InstrumentAlias]]:
        definitions = (await db.execute(select(InstrumentDefinition).where(
            InstrumentDefinition.raw_symbol == "AAPL",
        ))).scalars().all()
        aliases = (await db.execute(select(InstrumentAlias).where(or_(
            InstrumentAlias.alias_string.like("AAPL.%"),
            InstrumentAlias.instrument_uid.in_([row.instrument_uid for row in definitions]),
        )))).scalars().all()
        return definitions, aliases

    expected_definition = {
        "raw_symbol": "AAPL", "listing_venue": "NASDAQ", "routing_venue": "NASDAQ",
        "asset_class": "equity", "provider": "databento", "lifecycle_state": "active",
        "trading_hours": None, "roll_policy": None, "continuous_pattern": None,
        "hidden_from_inventory": False,
    }
    expected_alias = {
        "alias_string": "AAPL.NASDAQ", "provider": "databento", "venue_format": "exchange_name",
        "source_venue_raw": None, "effective_from": "1900-01-01", "effective_to": None,
    }

    def bind(
        definitions: Sequence[InstrumentDefinition], aliases: Sequence[InstrumentAlias],
    ) -> dict[str, Any]:
        if (len(definitions) != 1 or len(aliases) != 1
                or definition_metadata(definitions[0]) != expected_definition
                or alias_metadata(aliases[0]) != expected_alias
                or aliases[0].instrument_uid != definitions[0].instrument_uid):
            raise ValueError("conflicting or unmarked AAPL registry metadata refused")
        return {
            "fixture_id": FIXTURE_ID, "manifest_sha256": manifest_hash(manifest),
            "instrument_uid": str(definitions[0].instrument_uid), "alias_id": str(aliases[0].id),
            "definition": expected_definition, "alias": expected_alias,
            "origin": "synthetic", "live_qualified": False,
        }

    definitions, aliases = await rows()
    if definitions or aliases:
        current = bind(definitions, aliases)
        if binding != current:
            raise ValueError("conflicting or unmarked AAPL registry binding refused")
        return current
    if binding is not None:
        raise ValueError("conflicting registry binding exists without its owned rows")
    await make_security_master(db)._upsert_definition_and_alias(
        raw_symbol="AAPL", listing_venue="NASDAQ", routing_venue="NASDAQ", asset_class="equity",
        alias_string="AAPL.NASDAQ", provider="databento", venue_format="exchange_name",
    )
    return bind(*(await rows()))


async def run_bootstrap(data_root: Path, symbols: tuple[str, ...]) -> dict[str, Any]:
    from msai.core.database import async_session_factory, engine

    manifest, _ = prepare_fixture(symbols)
    check_fixture_files(data_root, manifest)
    binding_path = data_root / REGISTRY_NAME
    binding = json.loads(binding_path.read_text()) if binding_path.exists() else None
    try:
        async with async_session_factory() as db, db.begin():
            new_binding = await bootstrap_registry(db, manifest, binding)
        seed_fixture(data_root, symbols)
        binding_path.write_bytes(canonical_bytes(new_binding))
    finally:
        await engine.dispose()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--symbols", nargs="+", choices=SYMBOLS, default=list(SYMBOLS))
    parser.add_argument("--bootstrap-registry", action="store_true")
    parser.add_argument("--isolated-research", action="store_true")
    args = parser.parse_args()
    try:
        if args.bootstrap_registry:
            # Check explicit environment before imports load project settings/.env or any I/O.
            validate_registry_target(
                os.environ.get("ENVIRONMENT", ""), os.environ.get("DATABASE_URL", ""),
                args.isolated_research,
            )
            manifest = asyncio.run(run_bootstrap(args.data_root, tuple(args.symbols)))
        else:
            manifest = seed_fixture(args.data_root, tuple(args.symbols))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Fixture setup refused: {exc}\n")
    except Exception as exc:
        # Do not echo provider/DB exception details or configuration values into setup logs.
        parser.exit(2, f"Fixture setup failed ({type(exc).__name__}); existing data preserved\n")
    print(canonical_bytes(manifest).decode(), end="")


if __name__ == "__main__":
    main()
