"""MSAI operator CLI — organized into sub-apps per functional area.

The CLI is structured as a root Typer app with eight sub-apps (seven
functional + ``system``), each mapping to a functional area of the
platform.  Most sub-commands are thin HTTP wrappers around
``/api/v1/...`` endpoints so the CLI and dashboard stay in lock-step
(single source of truth on the server).  Data-ingestion commands call
the ingestion service directly — they need direct Parquet access and
run fine without a running API server.

Command tree::

    msai health                         top-level health check
    msai ingest ...                     historical data ingest
    msai ingest-daily ...               daily incremental ingest
    msai data-status                    storage stats

    msai strategy list                  registered strategies
    msai strategy show <id>             one strategy
    msai strategy validate <id>         load a strategy file end-to-end

    msai backtest run ...               enqueue a backtest
    msai backtest history               last-N backtest rows
    msai backtest show <id>             one backtest + metrics

    msai research list                  research jobs
    msai research show <id>             one research job
    msai research cancel <id>           cancel a running job

    msai live start ...                 deploy a strategy
    msai live stop <id>                 stop one deployment
    msai live status                    all deployments
    msai live kill-all                  emergency halt

    msai graduation list                graduation candidates
    msai graduation show <id>           one candidate + transitions

    msai portfolio list                 portfolios
    msai portfolio runs                 all portfolio runs
    msai portfolio show <id>            one portfolio
    msai portfolio run <id> ...         trigger a portfolio backtest

    msai account summary                IB account summary
    msai account positions              IB portfolio
    msai account health                 IB gateway status

    msai system health                  overall platform health

Auth: commands that hit the API send ``X-API-Key`` from ``$MSAI_API_KEY``
or the settings-level key — matches the backend's dual-mode auth in
``core/auth.py``.  Override the base URL with ``$MSAI_API_URL``.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import typer

from msai.core.config import settings
from msai.core.logging import get_logger, setup_logging
from msai.services.data_ingestion import DataIngestionService
from msai.services.parquet_store import ParquetStore

setup_logging(settings.environment)
log = get_logger(__name__)

# ----------------------------------------------------------------------
# Typer app tree
# ----------------------------------------------------------------------

app = typer.Typer(name="msai", help="MSAI v2 — Personal Hedge Fund Platform CLI")

strategy_app = typer.Typer(help="Strategy registry commands")
backtest_app = typer.Typer(help="Backtest run + history commands")
research_app = typer.Typer(help="Research job commands (sweeps, walk-forward)")
live_app = typer.Typer(help="Live/paper trading commands")
graduation_app = typer.Typer(help="Graduation pipeline commands")
portfolio_app = typer.Typer(help="Portfolio management + combined backtest commands")
account_app = typer.Typer(help="IB account commands")
system_app = typer.Typer(help="Platform health + diagnostics")

app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
app.add_typer(live_app, name="live")
app.add_typer(graduation_app, name="graduation")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(account_app, name="account")
app.add_typer(system_app, name="system")


# ----------------------------------------------------------------------
# HTTP helper — every API-backed command shares this
# ----------------------------------------------------------------------

_DEFAULT_API_BASE = "http://localhost:8000"


def _api_base() -> str:
    """Base URL for the MSAI API — override via ``MSAI_API_URL`` env."""
    return os.environ.get("MSAI_API_URL") or _DEFAULT_API_BASE


def _api_headers() -> dict[str, str]:
    """Request headers — attaches the API key when configured."""
    key = os.environ.get("MSAI_API_KEY") or settings.msai_api_key
    if key:
        return {"X-API-Key": key}
    return {}


def _fail(message: str, *, code: int = 1) -> None:
    """Print an error message to stderr and exit with non-zero code."""
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _api_call(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Make an authenticated request against the MSAI API.

    Fails the CLI with a clear error message on connection failure or
    non-2xx response; callers that want to inspect a specific status
    should catch :class:`typer.Exit` and re-raise.
    """
    url = f"{_api_base()}{path}"
    try:
        response = httpx.request(
            method,
            url,
            json=json_body,
            params=params,
            headers=_api_headers(),
            timeout=timeout,
        )
    except httpx.ConnectError:
        _fail(f"Connection refused — is the backend running at {_api_base()}?")
    if not response.is_success:
        _fail(f"API error ({response.status_code}): {response.text}")
    return response


def _emit_json(payload: object) -> None:
    """Render a Python value as pretty JSON on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


# ======================================================================
# Top-level: ingest + status
# ======================================================================


@app.command("health")
def health() -> None:
    """Quick CLI → backend round-trip check."""
    response = _api_call("GET", "/health", timeout=5.0)
    _emit_json(response.json())


@app.command("ingest")
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
        _fail("no symbols provided")

    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))
    typer.echo(f"Ingesting {asset} {symbol_list} from {start} to {end}...")
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
    _emit_json(result)


@app.command("ingest-daily")
def ingest_daily(
    asset: str = typer.Argument(..., help="Asset class (stocks, equities, futures, crypto)"),
    symbols: str = typer.Argument(
        ..., help="Comma-separated tickers (or 'all' to use stored symbols)"
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
            _fail(f"no existing symbols found for asset class '{asset}'")
    else:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]

    service = DataIngestionService(store)
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
    _emit_json(result)


@app.command("data-status")
def data_status() -> None:
    """Show storage stats, ingestion history, and data summary."""
    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))
    _emit_json(service.data_status())


# ======================================================================
# strategy sub-app
# ======================================================================


@strategy_app.command("list")
def strategy_list(
    limit: int = typer.Option(100, help="Max rows to return"),
) -> None:
    """List registered strategies."""
    response = _api_call("GET", "/api/v1/strategies/", params={"limit": limit})
    _emit_json(response.json())


@strategy_app.command("show")
def strategy_show(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
) -> None:
    """Show one strategy's details."""
    response = _api_call("GET", f"/api/v1/strategies/{strategy_id}")
    _emit_json(response.json())


@strategy_app.command("validate")
def strategy_validate(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
) -> None:
    """Validate that a strategy file can be loaded end-to-end."""
    response = _api_call("POST", f"/api/v1/strategies/{strategy_id}/validate")
    _emit_json(response.json())


# ======================================================================
# backtest sub-app
# ======================================================================


@backtest_app.command("run")
def backtest_run(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
    instruments: str = typer.Argument(..., help="Comma-separated instrument IDs"),
    start: str = typer.Argument(..., help="Start date YYYY-MM-DD"),
    end: str = typer.Argument(..., help="End date YYYY-MM-DD"),
    config_json: str = typer.Option("{}", help="Strategy config as a JSON string"),
) -> None:
    """Enqueue a backtest and print its job id.

    The job runs asynchronously in the arq backtest worker.  Poll status
    with ``msai backtest show <id>``.
    """
    instrument_list = [s.strip() for s in instruments.split(",") if s.strip()]
    if not instrument_list:
        _fail("no instruments provided")
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as exc:
        _fail(f"invalid --config-json: {exc}")
    payload = {
        "strategy_id": strategy_id,
        "config": config,
        "instruments": instrument_list,
        "start_date": start,
        "end_date": end,
    }
    response = _api_call("POST", "/api/v1/backtests/run", json_body=payload)
    _emit_json(response.json())


@backtest_app.command("history")
def backtest_history(
    limit: int = typer.Option(20, help="Max rows to return"),
) -> None:
    """List recent backtests with status + metrics."""
    response = _api_call("GET", "/api/v1/backtests/history", params={"limit": limit})
    _emit_json(response.json())


@backtest_app.command("show")
def backtest_show(
    backtest_id: str = typer.Argument(..., help="Backtest UUID"),
) -> None:
    """Show a backtest's current status + results (if complete)."""
    status_response = _api_call("GET", f"/api/v1/backtests/{backtest_id}/status")
    _emit_json(status_response.json())
    # Also pull results when the job has data — non-200 is fine
    # (pending/running jobs have no results yet), don't fail the CLI.
    results = httpx.get(
        f"{_api_base()}/api/v1/backtests/{backtest_id}/results",
        headers=_api_headers(),
        timeout=10.0,
    )
    if results.is_success:
        typer.echo("\n--- Results ---")
        _emit_json(results.json())


# ======================================================================
# research sub-app
# ======================================================================


@research_app.command("list")
def research_list(
    limit: int = typer.Option(20, help="Max rows to return"),
) -> None:
    """List research jobs (sweeps + walk-forward)."""
    response = _api_call("GET", "/api/v1/research/jobs", params={"limit": limit})
    _emit_json(response.json())


@research_app.command("show")
def research_show(
    job_id: str = typer.Argument(..., help="Research job UUID"),
) -> None:
    """Show one research job's progress + leaderboard."""
    response = _api_call("GET", f"/api/v1/research/jobs/{job_id}")
    _emit_json(response.json())


@research_app.command("cancel")
def research_cancel(
    job_id: str = typer.Argument(..., help="Research job UUID"),
) -> None:
    """Cancel a running research job."""
    response = _api_call("POST", f"/api/v1/research/jobs/{job_id}/cancel")
    _emit_json(response.json())


# ======================================================================
# live sub-app — mirrors the API contracts and preserves the tested
# behavior of the original flat commands (live-start, live-stop, etc.).
# ======================================================================


@live_app.command("start")
def live_start(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
    instruments: str = typer.Argument(..., help="Comma-separated instrument IDs"),
    paper: bool = typer.Option(True, help="Paper trading mode (default: True)"),
) -> None:
    """Start live/paper trading for a strategy."""
    instrument_list = [s.strip() for s in instruments.split(",") if s.strip()]
    if not instrument_list:
        _fail("no instruments provided")
    payload = {
        "strategy_id": strategy_id,
        "config": {},
        "instruments": instrument_list,
        "paper_trading": paper,
    }
    response = _api_call("POST", "/api/v1/live/start", json_body=payload)
    data = response.json()
    typer.echo(f"Deployment started: {data['id']} (status: {data['status']})")


@live_app.command("stop")
def live_stop(
    deployment_id: str = typer.Argument(..., help="Deployment UUID to stop"),
) -> None:
    """Stop a running deployment."""
    response = _api_call(
        "POST", "/api/v1/live/stop", json_body={"deployment_id": deployment_id}
    )
    data = response.json()
    typer.echo(f"Deployment {data['id']} stopped.")


@live_app.command("status")
def live_status() -> None:
    """Show all active deployments + risk-halt state."""
    response = _api_call("GET", "/api/v1/live/status", timeout=10.0)
    data = response.json()
    typer.echo(f"Risk halted: {data['risk_halted']}")
    typer.echo(f"Active nodes: {data['active_count']}")
    typer.echo(f"Deployments ({len(data['deployments'])}):")
    for d in data["deployments"]:
        mode = "PAPER" if d["paper_trading"] else "LIVE"
        typer.echo(
            f"  [{mode}] {d['id']}  status={d['status']}  instruments={d['instruments']}"
        )
    if not data["deployments"]:
        typer.echo("  (none)")


@live_app.command("kill-all")
def live_kill_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Emergency-stop all strategies.  Requires confirmation unless --yes."""
    if not yes:
        typer.confirm(
            "Are you sure you want to STOP ALL running strategies?", abort=True
        )
    response = _api_call("POST", "/api/v1/live/kill-all")
    data = response.json()
    typer.echo(
        f"Stopped {data['stopped']} strategies. Risk halted: {data['risk_halted']}"
    )


# ======================================================================
# graduation sub-app
# ======================================================================


@graduation_app.command("list")
def graduation_list(
    stage: str = typer.Option("", help="Filter by stage (discovery/paper/incubation/promoted)"),
    limit: int = typer.Option(50, help="Max rows to return"),
) -> None:
    """List graduation candidates, optionally filtered by stage."""
    params: dict[str, object] = {"limit": limit}
    if stage:
        params["stage"] = stage
    response = _api_call("GET", "/api/v1/graduation/candidates", params=params)
    _emit_json(response.json())


@graduation_app.command("show")
def graduation_show(
    candidate_id: str = typer.Argument(..., help="Candidate UUID"),
) -> None:
    """Show one candidate + its stage-transition audit trail."""
    response = _api_call("GET", f"/api/v1/graduation/candidates/{candidate_id}")
    _emit_json(response.json())


# ======================================================================
# portfolio sub-app
# ======================================================================


@portfolio_app.command("list")
def portfolio_list(
    limit: int = typer.Option(50, help="Max rows to return"),
) -> None:
    """List portfolios."""
    response = _api_call("GET", "/api/v1/portfolios", params={"limit": limit})
    _emit_json(response.json())


@portfolio_app.command("runs")
def portfolio_runs(
    portfolio_id: str = typer.Option("", help="Filter to one portfolio's runs"),
    limit: int = typer.Option(50, help="Max rows to return"),
) -> None:
    """List portfolio backtest runs, optionally filtered by portfolio."""
    params: dict[str, object] = {"limit": limit}
    if portfolio_id:
        params["portfolio_id"] = portfolio_id
    response = _api_call("GET", "/api/v1/portfolios/runs", params=params)
    _emit_json(response.json())


@portfolio_app.command("show")
def portfolio_show(
    portfolio_id: str = typer.Argument(..., help="Portfolio UUID"),
) -> None:
    """Show one portfolio's detail."""
    response = _api_call("GET", f"/api/v1/portfolios/{portfolio_id}")
    _emit_json(response.json())


@portfolio_app.command("run")
def portfolio_run(
    portfolio_id: str = typer.Argument(..., help="Portfolio UUID"),
    start: str = typer.Argument(..., help="Start date YYYY-MM-DD"),
    end: str = typer.Argument(..., help="End date YYYY-MM-DD"),
    max_parallelism: int = typer.Option(
        0, help="Parallel candidate backtests (0 = backend default)"
    ),
) -> None:
    """Trigger a portfolio-level backtest run."""
    payload: dict[str, object] = {"start_date": start, "end_date": end}
    if max_parallelism > 0:
        payload["max_parallelism"] = max_parallelism
    response = _api_call(
        "POST", f"/api/v1/portfolios/{portfolio_id}/runs", json_body=payload
    )
    _emit_json(response.json())


# ======================================================================
# account sub-app (IB)
# ======================================================================


@account_app.command("summary")
def account_summary() -> None:
    """Show IB account summary (cash, net liquidation, buying power)."""
    response = _api_call("GET", "/api/v1/account/summary")
    _emit_json(response.json())


@account_app.command("positions")
def account_positions() -> None:
    """Show IB account portfolio positions."""
    response = _api_call("GET", "/api/v1/account/portfolio")
    _emit_json(response.json())


@account_app.command("health")
def account_health() -> None:
    """Show IB gateway connection health."""
    response = _api_call("GET", "/api/v1/account/health")
    _emit_json(response.json())


# ======================================================================
# system sub-app
# ======================================================================


@system_app.command("health")
def system_health() -> None:
    """Compound health check across API + live + account surfaces."""
    parts: dict[str, object] = {}
    for label, path in (
        ("api", "/health"),
        ("ready", "/ready"),
        ("live", "/api/v1/live/status"),
        ("account", "/api/v1/account/health"),
    ):
        try:
            response = httpx.get(
                f"{_api_base()}{path}", headers=_api_headers(), timeout=5.0
            )
            parts[label] = {
                "status_code": response.status_code,
                "ok": response.is_success,
            }
        except httpx.ConnectError:
            parts[label] = {"error": "connection refused"}
    _emit_json(parts)


if __name__ == "__main__":
    app()
