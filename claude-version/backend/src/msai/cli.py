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
from urllib.parse import quote

import httpx
import typer

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger, setup_logging
from msai.services.data_ingestion import DataIngestionService
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.service import SecurityMaster
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
instruments_app = typer.Typer(
    name="instruments",
    help="Instrument registry operations",
    rich_markup_mode="rich",
)

app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
app.add_typer(live_app, name="live")
app.add_typer(graduation_app, name="graduation")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(account_app, name="account")
app.add_typer(system_app, name="system")
app.add_typer(instruments_app, name="instruments")


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

    Fails the CLI with a clear error message on connection failure,
    request timeout, generic request errors, or non-2xx response.
    Callers that want to inspect a specific status should catch
    :class:`typer.Exit` and re-raise.
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
    except httpx.TimeoutException as exc:
        # IB-connecting paths (live start, account health on cold IB) can
        # exceed the default 30s.  Surface the timeout cleanly instead of
        # leaking an httpx traceback; operators can retry or raise the
        # per-command timeout if they know the op is slow.
        _fail(f"Request timed out after {timeout}s against {url} ({type(exc).__name__})")
    except httpx.RequestError as exc:
        # Catchall for DNS, TLS, invalid URL, proxy failures — anything
        # httpx raises before it gets a response.
        _fail(f"Request failed: {type(exc).__name__}: {exc}")
    if not response.is_success:
        _fail(f"API error ({response.status_code}): {response.text}")
    return response


def _emit_json(payload: object) -> None:
    """Render a Python value as pretty JSON on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _url_id(value: str) -> str:
    """URL-encode an ID before interpolating into a path.

    Prevents a malicious or typo-ed ID containing ``/``, ``..``, ``?``
    from escaping the intended route.  Without this, ``httpx`` would
    normalize ``/api/v1/strategies/../account/summary`` and silently
    redirect an authenticated request to a different endpoint.
    """
    return quote(str(value), safe="")


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
def strategy_list() -> None:
    """List registered strategies.

    The backend endpoint does not paginate today — all registered
    strategies are returned.  When pagination is added server-side, a
    matching ``--page-size`` option goes here.
    """
    response = _api_call("GET", "/api/v1/strategies/")
    _emit_json(response.json())


@strategy_app.command("show")
def strategy_show(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
) -> None:
    """Show one strategy's details."""
    response = _api_call("GET", f"/api/v1/strategies/{_url_id(strategy_id)}")
    _emit_json(response.json())


@strategy_app.command("validate")
def strategy_validate(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
) -> None:
    """Validate that a strategy file can be loaded end-to-end."""
    response = _api_call("POST", f"/api/v1/strategies/{_url_id(strategy_id)}/validate")
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
    page: int = typer.Option(1, help="Page number (1-indexed)"),
    page_size: int = typer.Option(20, help="Rows per page (backend max is 100)"),
) -> None:
    """List recent backtests with status + metrics.

    Uses the API's ``page`` / ``page_size`` pagination — ``limit`` is
    NOT an accepted query param and is silently ignored by FastAPI, so
    naming the CLI flags to match the server contract keeps both honest.
    """
    response = _api_call(
        "GET",
        "/api/v1/backtests/history",
        params={"page": page, "page_size": page_size},
    )
    _emit_json(response.json())


@backtest_app.command("show")
def backtest_show(
    backtest_id: str = typer.Argument(..., help="Backtest UUID"),
) -> None:
    """Show a backtest's current status + results (if complete)."""
    safe_id = _url_id(backtest_id)
    status_response = _api_call("GET", f"/api/v1/backtests/{safe_id}/status")
    _emit_json(status_response.json())
    # Also pull results when the job has data — non-200 is fine
    # (pending/running jobs have no results yet), don't fail the CLI.
    # Swallow transport errors too: if results are unavailable the status
    # above is the useful output and we shouldn't fail on a flaky GET.
    try:
        results = httpx.get(
            f"{_api_base()}/api/v1/backtests/{safe_id}/results",
            headers=_api_headers(),
            timeout=10.0,
        )
    except httpx.RequestError:
        return
    if results.is_success:
        typer.echo("\n--- Results ---")
        _emit_json(results.json())


# ======================================================================
# research sub-app
# ======================================================================


@research_app.command("list")
def research_list(
    page: int = typer.Option(1, help="Page number (1-indexed)"),
    page_size: int = typer.Option(20, help="Rows per page (backend max is 100)"),
) -> None:
    """List research jobs (sweeps + walk-forward)."""
    response = _api_call(
        "GET", "/api/v1/research/jobs", params={"page": page, "page_size": page_size}
    )
    _emit_json(response.json())


@research_app.command("show")
def research_show(
    job_id: str = typer.Argument(..., help="Research job UUID"),
) -> None:
    """Show one research job's progress + leaderboard."""
    response = _api_call("GET", f"/api/v1/research/jobs/{_url_id(job_id)}")
    _emit_json(response.json())


@research_app.command("cancel")
def research_cancel(
    job_id: str = typer.Argument(..., help="Research job UUID"),
) -> None:
    """Cancel a running research job."""
    response = _api_call("POST", f"/api/v1/research/jobs/{_url_id(job_id)}/cancel")
    _emit_json(response.json())


# ======================================================================
# live sub-app — mirrors the API contracts and preserves the tested
# behavior of the original flat commands (live-start, live-stop, etc.).
# ======================================================================


@live_app.command("start")
def live_start(
    portfolio_revision_id: str = typer.Argument(..., help="Portfolio revision UUID"),
    account_id: str = typer.Argument(..., help="IB account id (e.g. DU1234567)"),
    paper: bool = typer.Option(True, help="Paper trading mode (default: True)"),
    ib_login_key: str = typer.Option(
        "", help="IB login username (optional, server derives if empty)"
    ),
) -> None:
    """Deploy a portfolio revision to live/paper trading."""
    payload: dict[str, object] = {
        "portfolio_revision_id": portfolio_revision_id,
        "account_id": account_id,
        "paper_trading": paper,
    }
    if ib_login_key:
        payload["ib_login_key"] = ib_login_key
    response = _api_call("POST", "/api/v1/live/start-portfolio", json_body=payload)
    data = response.json()
    dep_id = data.get("id", "unknown")
    dep_status = data.get("status", "unknown")
    typer.echo(f"Deployment started: {dep_id} (status: {dep_status})")


@live_app.command("stop")
def live_stop(
    deployment_id: str = typer.Argument(..., help="Deployment UUID to stop"),
) -> None:
    """Stop a running deployment."""
    response = _api_call("POST", "/api/v1/live/stop", json_body={"deployment_id": deployment_id})
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
        typer.echo(f"  [{mode}] {d['id']}  status={d['status']}  instruments={d['instruments']}")
    if not data["deployments"]:
        typer.echo("  (none)")


@live_app.command("kill-all")
def live_kill_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Emergency-stop all strategies.  Requires confirmation unless --yes."""
    if not yes:
        typer.confirm("Are you sure you want to STOP ALL running strategies?", abort=True)
    response = _api_call("POST", "/api/v1/live/kill-all")
    data = response.json()
    typer.echo(f"Stopped {data['stopped']} strategies. Risk halted: {data['risk_halted']}")


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
    """Show one candidate + its stage-transition audit trail.

    The candidate detail and the transitions live on separate endpoints
    (``/candidates/{id}`` and ``/candidates/{id}/transitions``).  We
    fetch both and merge them into a single JSON object so operators
    see the full audit history in one command — fulfilling the
    docstring's promise.
    """
    safe_id = _url_id(candidate_id)
    candidate = _api_call("GET", f"/api/v1/graduation/candidates/{safe_id}").json()
    try:
        transitions_response = httpx.get(
            f"{_api_base()}/api/v1/graduation/candidates/{safe_id}/transitions",
            headers=_api_headers(),
            timeout=10.0,
        )
        transitions = transitions_response.json() if transitions_response.is_success else []
    except httpx.RequestError:
        # Transport error on the transitions fetch isn't fatal — the
        # candidate body still has value.
        transitions = []
    _emit_json({"candidate": candidate, "transitions": transitions})


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
    response = _api_call("GET", f"/api/v1/portfolios/{_url_id(portfolio_id)}")
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
        "POST",
        f"/api/v1/portfolios/{_url_id(portfolio_id)}/runs",
        json_body=payload,
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
    """Compound health check across API + live + account surfaces.

    Each probe is independent — a failure on one doesn't mask others.
    Timeouts and request errors degrade to ``{"error": "..."}`` entries
    so the operator can see which surfaces are reachable.

    Note on ``/api/v1/account/health``: it returns HTTP 200 even when
    the IB gateway is down, with ``status: "unhealthy"`` in the body.
    Trusting ``response.is_success`` alone would report the account
    surface healthy in exactly the outage case this command exists to
    detect — we merge the response body and treat ``status != "healthy"``
    as ``ok: false``.
    """

    def _parse_probe(response: httpx.Response, body_ok_fn) -> dict[str, object]:
        """Derive ``ok`` from the response body when needed."""
        body: object | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        body_ok = body_ok_fn(body) if body is not None else response.is_success
        return {
            "status_code": response.status_code,
            "ok": bool(response.is_success and body_ok),
            "body": body,
        }

    probes: list[tuple[str, str, object]] = [
        # label, path, body-ok predicate
        ("api", "/health", lambda _b: True),
        ("ready", "/ready", lambda _b: True),
        ("live", "/api/v1/live/status", lambda _b: True),
        # IB health returns 200 even when the gateway is down; derive
        # ok from the body status + gateway_connected fields.
        (
            "account",
            "/api/v1/account/health",
            lambda b: (
                isinstance(b, dict)
                and b.get("status") == "healthy"
                and bool(b.get("gateway_connected"))
            ),
        ),
    ]
    parts: dict[str, object] = {}
    for label, path, body_ok_fn in probes:
        try:
            response = httpx.get(f"{_api_base()}{path}", headers=_api_headers(), timeout=5.0)
            parts[label] = _parse_probe(response, body_ok_fn)
        except httpx.ConnectError:
            parts[label] = {"error": "connection refused"}
        except httpx.TimeoutException:
            parts[label] = {"error": "timeout"}
        except httpx.RequestError as exc:
            parts[label] = {"error": f"{type(exc).__name__}: {exc}"}
    _emit_json(parts)


# ======================================================================
# instruments sub-app
# ======================================================================


@instruments_app.command("refresh")
def instruments_refresh(
    symbols: str = typer.Option(
        ...,
        "--symbols",
        help="Comma-separated symbols (e.g. ``AAPL,ES.Z.5``)",
    ),
    provider: str = typer.Option(
        "databento",
        "--provider",
        help=(
            "Provider to pre-warm: ``databento`` (Parquet ``.Z.N`` "
            "continuous futures via DatabentoClient) or "
            "``interactive_brokers`` (short-lived IB Gateway client; "
            "uses ``IB_INSTRUMENT_CLIENT_ID=999`` by default — see "
            "nautilus.md gotcha #3 for the collision contract)."
        ),
    ),
    start: str = typer.Option(
        "2024-01-01",
        "--start",
        help="Definition window start (``YYYY-MM-DD``) — used for ``.Z.N`` fetch",
    ),
    end: str = typer.Option(
        "",
        "--end",
        help="Definition window end (``YYYY-MM-DD``) — defaults to today UTC",
    ),
    dataset: str = typer.Option(
        "GLBX.MDP3",
        "--dataset",
        help="Databento dataset for ``.Z.N`` cold-miss synthesis",
    ),
) -> None:
    """Pre-warm the instrument registry so later deployments never hit a
    cold-miss at bar-event time.

    This is the PRD §47-48 pre-warm tool. Operators run it before
    deploying a new strategy so:

    * Backtest resolve (:meth:`SecurityMaster.resolve_for_backtest`)
      succeeds on the ``.Z.N`` continuous-futures path by downloading
      the Databento ``definition`` payload and upserting the registry
      row.
    * Live resolve (:meth:`SecurityMaster.resolve_for_live`) — for
      ``--provider interactive_brokers`` — connects a short-lived
      Nautilus IB client, qualifies each requested symbol against IB
      Gateway, upserts registry rows, then disconnects. Day-1 scope
      is the closed universe ``resolve_for_live`` supports today:
      ``AAPL``, ``MSFT``, ``SPY``, ``EUR/USD``, ``ES``.

    Settings read (for ``--provider interactive_brokers``):

    * ``IB_HOST`` / ``IB_PORT`` / ``IB_ACCOUNT_ID`` — gateway target
      (paper port 4002/4004 + ``DU*``/``DF*`` account, or live port
      4001/4003 + non-``D`` account; gotcha #6 mismatch guard fires
      at preflight).
    * ``IB_CONNECT_TIMEOUT_SECONDS`` (default 5) — gateway-reachability
      probe.
    * ``IB_REQUEST_TIMEOUT_SECONDS`` (default 30) — per-symbol
      qualification round-trip.
    * ``IB_INSTRUMENT_CLIENT_ID`` (default 999) — surfaced in every
      preflight log; see nautilus.md gotcha #3 for the collision
      contract.
    """
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        _fail("no symbols provided")

    if provider == "interactive_brokers":
        from msai.services.nautilus.live_instrument_bootstrap import (
            canonical_instrument_id,
            phase_1_paper_symbols,
        )

        # Build an exact accepted-alias set per supported root. Each
        # root admits three shapes: bare (``AAPL``), stable dotted
        # (``AAPL.NASDAQ``, ``ES.CME``), and the concrete canonical
        # form that canonical_instrument_id produces (``ESM6.CME``
        # for today's front-month ES). PRD US-006: operators must
        # be able to feed the CLI's own ``resolved`` output back in
        # as a re-run. Exact membership avoids the permissive-strip
        # trap (e.g. ``SPY.NASDAQ`` silently normalizing to ``SPY``
        # via generic suffix stripping).
        known = phase_1_paper_symbols()
        accepted: dict[str, str] = {}
        for root in known:
            accepted[root] = root
            canonical = canonical_instrument_id(root)
            accepted[canonical] = root
            stable = f"{root}.{canonical.rsplit('.', 1)[1]}"
            accepted[stable] = root

        normalized: list[str] = []
        unknown: list[str] = []
        for s in symbol_list:
            if s in accepted:
                normalized.append(accepted[s])
            else:
                unknown.append(s)
        if unknown:
            _fail(
                f"symbol(s) {unknown} not in the closed universe for "
                f"--provider interactive_brokers. Supported inputs: "
                f"{sorted(accepted)}. Options outside this list "
                f"require the live-path wiring PR (follow-up)."
            )
        symbol_list = normalized

        # Port/account mode consistency (gotcha #6 guard). Runs BEFORE
        # any IB connection so a misconfigured operator can't even
        # burn the client_id slot trying.
        from msai.services.nautilus.ib_port_validator import (
            validate_port_account_consistency,
        )

        try:
            validate_port_account_consistency(
                settings.ib_port,
                settings.ib_account_id,
            )
        except ValueError as exc:
            _fail(str(exc))

        # Log the resolved tuple so operators can grep `docker logs`
        # if anything downstream goes wrong.
        typer.echo(
            f"Pre-warming IB registry: host={settings.ib_host} "
            f"port={settings.ib_port} "
            f"account={settings.ib_account_id.strip()} "
            f"client_id={settings.ib_instrument_client_id} "
            f"connect_timeout={settings.ib_connect_timeout_seconds}s "
            f"request_timeout={settings.ib_request_timeout_seconds}s"
        )

        try:
            resolved = asyncio.run(_run_ib_resolve_for_live(symbol_list))
        except _IBGatewayUnreachableError as exc:
            _fail(str(exc))
        _emit_json({"provider": provider, "resolved": resolved})
        return

    if provider != "databento":
        raise typer.BadParameter(
            f"unsupported provider {provider!r} — use 'databento' "
            "(or 'interactive_brokers' once the follow-up PR lands)."
        )

    # Databento path.
    api_key = os.environ.get("DATABENTO_API_KEY") or settings.databento_api_key
    if not api_key:
        raise typer.BadParameter(
            "DATABENTO_API_KEY is not set — export the env var (or add it to "
            "the backend's settings) before running `msai instruments refresh "
            "--provider databento`.  The command cannot fetch a `.Z.N` "
            "continuous-futures definition without the API key.",
        )

    async def _run() -> list[str]:
        async with async_session_factory() as session:
            databento_client = DatabentoClient(api_key)
            security_master = SecurityMaster(
                qualifier=None,
                db=session,
                databento_client=databento_client,
            )
            try:
                resolved = await security_master.resolve_for_backtest(
                    symbol_list,
                    start=start,
                    end=end or None,
                    dataset=dataset,
                )
            except Exception:
                # Roll back any partial writes before the context exits so
                # we never leave half-upserted rows behind on failure.
                await session.rollback()
                raise
            # SecurityMaster._upsert_definition_and_alias only flushes —
            # without an explicit commit the async session rolls back on
            # context exit and the registry is unchanged despite a
            # success-looking CLI output.
            await session.commit()
            return resolved

    typer.echo(f"Pre-warming registry for {symbol_list} via Databento...")
    resolved = asyncio.run(_run())
    _emit_json({"provider": provider, "resolved": resolved})


class _IBGatewayUnreachableError(RuntimeError):
    """Raised by ``_run_ib_resolve_for_live`` when the caller-side
    ``asyncio.wait_for`` fence on ``client._is_client_ready`` fires.

    Caught at the CLI boundary and converted to ``_fail(str(exc))`` so
    the operator sees a clear hint naming the relevant env vars and
    the paper/live mismatch trap.
    """


async def _run_ib_resolve_for_live(symbol_list: list[str]) -> list[str]:
    """Short-lived Nautilus IB client lifecycle wrapping
    :meth:`SecurityMaster.resolve_for_live`.

    Lifecycle:

    1. Cap the IB client's internal reconnect loop to one attempt
       (``IB_MAX_CONNECTION_ATTEMPTS=1``) BEFORE constructing the
       client. ``InteractiveBrokersClient._connect`` catches all
       exceptions and ``_start_async``'s outer ``while not
       _is_ib_connected`` loop retries forever in the background;
       capping attempts makes the retry loop bounded.
    2. Build MessageBus + Cache + LiveClock.
    3. ``get_cached_ib_client(...)`` — this ALREADY calls
       ``client.start()`` internally at construction. Do NOT call
       ``client.start()`` again: it would schedule a second
       ``_start_async`` task racing the first.
    4. Connect fence: ``asyncio.wait_for`` on
       ``client._is_client_ready.wait()`` — the caller owns the
       timeout. Nautilus's ``wait_until_ready`` silently swallows
       ``TimeoutError`` and only logs, giving a "dead gateway looks
       ready" false-negative.
    5. ``get_cached_interactive_brokers_instrument_provider`` → wrap
       in the existing :class:`IBQualifier`.
    6. ``SecurityMaster.resolve_for_live(symbols)`` + commit. Upserts
       rows into ``instrument_definitions`` + ``instrument_aliases``.
    7. ``try/finally`` teardown: ``await client._stop_async()``
       DIRECTLY. The public ``client.stop()`` only schedules
       ``_stop_async`` as a task; awaiting it ourselves guarantees
       the TCP disconnect completes before the process exits (US-005:
       re-run within 60s without leaving a zombie ``client_id``
       slot). FSM state doesn't matter because we're exiting
       immediately.
    """
    import os

    # Cap the reconnect loop BEFORE client construction — the client
    # reads this env var on first call to `_start_async`; setting it
    # AFTER construction is too late.
    os.environ.setdefault("IB_MAX_CONNECTION_ATTEMPTS", "1")

    # Import Nautilus only inside the function so the CLI module stays
    # importable on machines without the IB extras (ruff / mypy in CI).
    from nautilus_trader.adapters.interactive_brokers.factories import (
        get_cached_ib_client,
        get_cached_interactive_brokers_instrument_provider,
    )
    from nautilus_trader.cache.cache import Cache  # type: ignore[import-not-found]
    from nautilus_trader.common.component import (  # type: ignore[import-not-found]
        LiveClock,
        MessageBus,
    )
    from nautilus_trader.model.identifiers import TraderId  # type: ignore[import-not-found]

    from msai.services.nautilus.live_instrument_bootstrap import (
        build_ib_instrument_provider_config,
    )
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier

    clock = LiveClock()
    trader_id = TraderId("MSAI-INSTRUMENTS-REFRESH")
    msgbus = MessageBus(trader_id=trader_id, clock=clock)
    cache = Cache()

    client = get_cached_ib_client(
        loop=asyncio.get_running_loop(),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        host=settings.ib_host,
        port=settings.ib_port,
        client_id=settings.ib_instrument_client_id,
        request_timeout_secs=settings.ib_request_timeout_seconds,
    )
    # NOTE: get_cached_ib_client ALREADY calls client.start() internally.
    # DO NOT call client.start() here — it would schedule a second
    # _start_async task racing the first.

    try:
        # Caller-side timeout fence — bypasses `wait_until_ready`
        # which silently swallows TimeoutError and only logs.
        try:
            await asyncio.wait_for(
                client._is_client_ready.wait(),
                timeout=settings.ib_connect_timeout_seconds,
            )
        except TimeoutError as exc:
            raise _IBGatewayUnreachableError(
                f"IB Gateway not reachable at {settings.ib_host}:"
                f"{settings.ib_port} within "
                f"{settings.ib_connect_timeout_seconds}s. Check: "
                f"(a) gateway container running, "
                f"(b) IB_PORT matches IB_ACCOUNT_ID prefix "
                f"(DU/DF* → paper 4002/4004, U* → live 4001/4003), "
                f"(c) IB_INSTRUMENT_CLIENT_ID={settings.ib_instrument_client_id} "
                f"not colliding with an active subprocess."
            ) from exc

        provider_cfg = build_ib_instrument_provider_config(symbol_list)
        provider = get_cached_interactive_brokers_instrument_provider(
            client=client,
            clock=clock,
            config=provider_cfg,
        )
        qualifier = IBQualifier(provider)

        async with async_session_factory() as session:
            sm = SecurityMaster(qualifier=qualifier, db=session)
            try:
                resolved = await sm.resolve_for_live(symbol_list)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        return resolved
    finally:
        # Await `_stop_async` DIRECTLY. `client.stop()` would schedule
        # it as a task — if we then also awaited it we'd run the
        # coroutine twice. Going direct sidesteps the race; FSM state
        # doesn't matter because the process exits immediately after.
        try:
            await client._stop_async()
        except Exception:  # pragma: no cover — best-effort teardown
            log.warning("ib_refresh_teardown_error", exc_info=True)


if __name__ == "__main__":
    app()
