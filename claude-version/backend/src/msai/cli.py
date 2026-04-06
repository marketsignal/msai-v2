"""MSAI CLI for data ingestion, live trading control, and status reporting.

Provides command-line access to data ingestion workflows, live trading
management, and storage diagnostics.  Installed as the ``msai`` console
script via pyproject.toml.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import typer

from msai.core.config import settings
from msai.core.logging import get_logger, setup_logging
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.data_ingestion import DataIngestionService
from msai.services.parquet_store import ParquetStore

setup_logging(settings.environment)
log = get_logger(__name__)

app = typer.Typer(name="msai", help="MSAI v2 -- Personal Hedge Fund Platform CLI")


@app.command()
def ingest(
    asset: str = typer.Argument(..., help="Asset class (stocks, futures, crypto)"),
    symbols: str = typer.Argument(..., help="Comma-separated ticker symbols"),
    start: str = typer.Argument(..., help="Start date YYYY-MM-DD"),
    end: str = typer.Argument(..., help="End date YYYY-MM-DD"),
) -> None:
    """Download historical market data for the given symbols and date range."""
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        typer.echo("Error: no symbols provided", err=True)
        raise typer.Exit(code=1)

    store = ParquetStore(settings.data_root)
    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None

    databento = None
    if settings.databento_api_key:
        from msai.services.data_sources.databento_client import DatabentoClient

        databento = DatabentoClient(settings.databento_api_key)

    service = DataIngestionService(store, polygon=polygon, databento=databento)

    typer.echo(f"Ingesting {asset} data for {symbol_list} from {start} to {end}...")
    results = asyncio.run(service.ingest_historical(asset, symbol_list, start, end))

    for sym, rows in results.items():
        typer.echo(f"  {sym}: {rows} rows")

    total = sum(results.values())
    typer.echo(f"Done. Total rows written: {total}")


@app.command()
def ingest_daily(
    asset: str = typer.Argument(..., help="Asset class (stocks, futures, crypto)"),
    symbols: str = typer.Argument(
        ..., help="Comma-separated ticker symbols (or 'all' to use stored symbols)"
    ),
) -> None:
    """Download yesterday's data for incremental daily update."""
    store = ParquetStore(settings.data_root)

    if symbols.lower() == "all":
        symbol_list = store.list_symbols(asset)
        if not symbol_list:
            typer.echo(f"No existing symbols found for asset class '{asset}'", err=True)
            raise typer.Exit(code=1)
    else:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]

    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None

    databento = None
    if settings.databento_api_key:
        from msai.services.data_sources.databento_client import DatabentoClient

        databento = DatabentoClient(settings.databento_api_key)

    service = DataIngestionService(store, polygon=polygon, databento=databento)

    typer.echo(f"Running daily ingest for {asset}: {symbol_list}")
    results = asyncio.run(service.ingest_daily(asset, symbol_list))

    for sym, rows in results.items():
        typer.echo(f"  {sym}: {rows} rows")

    total = sum(results.values())
    typer.echo(f"Done. Total rows written: {total}")


@app.command()
def data_status() -> None:
    """Show storage stats and data summary."""
    store = ParquetStore(settings.data_root)
    stats = store.get_storage_stats()

    typer.echo("Storage Statistics:")
    typer.echo(f"  Total files:  {stats['total_files']}")
    typer.echo(f"  Total size:   {stats['total_bytes']:,} bytes")

    if stats["asset_classes"]:
        typer.echo("\n  By asset class:")
        for ac, size in stats["asset_classes"].items():
            symbols = store.list_symbols(ac)
            typer.echo(f"    {ac}: {size:,} bytes  ({len(symbols)} symbols)")
    else:
        typer.echo("  No data found.")


_API_BASE = "http://localhost:8000"


@app.command()
def live_start(
    strategy: str = typer.Argument(..., help="Strategy UUID"),
    instruments: str = typer.Argument(..., help="Comma-separated instrument identifiers"),
    paper: bool = typer.Option(True, help="Paper trading mode (default: True)"),
) -> None:
    """Start live/paper trading for a strategy."""
    instrument_list = [s.strip() for s in instruments.split(",") if s.strip()]
    if not instrument_list:
        typer.echo("Error: no instruments provided", err=True)
        raise typer.Exit(code=1)

    payload = {
        "strategy_id": strategy,
        "config": {},
        "instruments": instrument_list,
        "paper_trading": paper,
    }

    response = httpx.post(f"{_API_BASE}/api/v1/live/start", json=payload, timeout=30.0)
    if response.status_code == 201:
        data = response.json()
        typer.echo(f"Deployment started: {data['id']} (status: {data['status']})")
    else:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(code=1)


@app.command()
def live_stop(
    deployment_id: str = typer.Argument(..., help="Deployment UUID to stop"),
) -> None:
    """Stop a running deployment."""
    payload = {"deployment_id": deployment_id}
    response = httpx.post(f"{_API_BASE}/api/v1/live/stop", json=payload, timeout=30.0)
    if response.status_code == 200:
        data = response.json()
        typer.echo(f"Deployment {data['id']} stopped.")
    else:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(code=1)


@app.command()
def live_status() -> None:
    """Show all active deployments."""
    response = httpx.get(f"{_API_BASE}/api/v1/live/status", timeout=10.0)
    if response.status_code != 200:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(code=1)

    data = response.json()
    typer.echo(f"Risk halted: {data['risk_halted']}")
    typer.echo(f"Active nodes: {data['active_count']}")
    typer.echo(f"Deployments ({len(data['deployments'])}):")
    for d in data["deployments"]:
        mode = "PAPER" if d["paper_trading"] else "LIVE"
        typer.echo(f"  [{mode}] {d['id']}  status={d['status']}  instruments={d['instruments']}")

    if not data["deployments"]:
        typer.echo("  (none)")


@app.command()
def live_kill_all() -> None:
    """Emergency stop all strategies."""
    typer.confirm("Are you sure you want to STOP ALL running strategies?", abort=True)
    response = httpx.post(f"{_API_BASE}/api/v1/live/kill-all", timeout=30.0)
    if response.status_code == 200:
        data = response.json()
        typer.echo(f"Stopped {data['stopped']} strategies. Risk halted: {data['risk_halted']}")
    else:
        typer.echo(f"Error ({response.status_code}): {response.text}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
