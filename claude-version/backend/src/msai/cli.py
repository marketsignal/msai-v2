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
from msai.services.data_ingestion import DataIngestionService
from msai.services.parquet_store import ParquetStore

setup_logging(settings.environment)
log = get_logger(__name__)

app = typer.Typer(name="msai", help="MSAI v2 -- Personal Hedge Fund Platform CLI")


@app.command()
def ingest(
    asset: str = typer.Argument(..., help="Asset class (stocks, equities, futures, crypto)"),
    symbols: str = typer.Argument(..., help="Comma-separated ticker symbols"),
    start: str = typer.Argument(..., help="Start date YYYY-MM-DD"),
    end: str = typer.Argument(..., help="End date YYYY-MM-DD"),
    provider: str = typer.Option("auto", help="Data provider: auto, databento, or polygon"),
    dataset: str = typer.Option("", help="Override default Databento dataset"),
    schema: str = typer.Option("", help="Override default Databento schema"),
) -> None:
    """Download historical market data for the given symbols and date range."""
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        typer.echo("Error: no symbols provided", err=True)
        raise typer.Exit(code=1)

    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))

    typer.echo(f"Ingesting {asset} data for {symbol_list} from {start} to {end}...")
    typer.echo(f"  provider={provider}, dataset={dataset or '(default)'}, schema={schema or '(default)'}")
    result = asyncio.run(
        service.ingest_historical(
            asset,
            symbol_list,
            start,
            end,
            provider=provider,
            dataset=dataset or None,
            schema=schema or None,
        )
    )

    typer.echo(json.dumps(result, indent=2))


@app.command()
def ingest_daily(
    asset: str = typer.Argument(..., help="Asset class (stocks, equities, futures, crypto)"),
    symbols: str = typer.Argument(
        ..., help="Comma-separated ticker symbols (or 'all' to use stored symbols)"
    ),
    provider: str = typer.Option("auto", help="Data provider: auto, databento, or polygon"),
    dataset: str = typer.Option("", help="Override default Databento dataset"),
    schema: str = typer.Option("", help="Override default Databento schema"),
) -> None:
    """Download yesterday's data for incremental daily update."""
    store = ParquetStore(str(settings.parquet_root))

    if symbols.lower() == "all":
        symbol_list = store.list_symbols(asset)
        if not symbol_list:
            typer.echo(f"No existing symbols found for asset class '{asset}'", err=True)
            raise typer.Exit(code=1)
    else:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]

    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))

    typer.echo(f"Running daily ingest for {asset}: {symbol_list}")
    result = asyncio.run(
        service.ingest_daily(
            asset,
            symbol_list,
            provider=provider,
            dataset=dataset or None,
            schema=schema or None,
        )
    )

    typer.echo(json.dumps(result, indent=2))


@app.command()
def data_status() -> None:
    """Show storage stats, ingestion history, and data summary."""
    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))
    typer.echo(json.dumps(service.data_status(), indent=2, default=str))


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
