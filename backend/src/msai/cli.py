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
import getpass
import json
import os
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from nautilus_trader.adapters.interactive_brokers.common import IBContract

import httpx
import typer

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger, setup_logging
from msai.services.data_ingestion import DataIngestionService
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.parquet_store import ParquetStore
from msai.services.smoke.config import SMOKE_CONFIGS, SmokeConfigName
from msai.services.symbol_onboarding.partition_index import make_refresh_callback

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
broker_app = typer.Typer(help="Broker account management")
system_app = typer.Typer(help="Platform health + diagnostics")
instruments_app = typer.Typer(
    name="instruments",
    help="Instrument registry operations",
    rich_markup_mode="rich",
)
alerts_app = typer.Typer(help="Operational alert history")
auth_app = typer.Typer(help="Authentication / current-user commands")
market_data_app = typer.Typer(help="Market data (Parquet-backed) bars, symbols, status, ingest")

app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
app.add_typer(live_app, name="live")
app.add_typer(graduation_app, name="graduation")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(account_app, name="account")
app.add_typer(broker_app, name="broker")
app.add_typer(system_app, name="system")
app.add_typer(instruments_app, name="instruments")
app.add_typer(alerts_app, name="alerts")
app.add_typer(auth_app, name="auth")
app.add_typer(market_data_app, name="market-data")


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


def _resolve_cli_account_payload(
    *,
    broker_account_id: str,
    account: str,
    ib_login_key: str,
    paper: bool,
    selector_pair_hint: str,
    account_flag_label: str,
    account_value_label: str,
) -> dict[str, str]:
    """Validate the either/or account selection (Task 5/6 contract) and return
    the account-bearing payload fields for ``POST /api/v1/live/start-portfolio``.

    Shared by ``live start`` and ``live start-portfolio`` so the either/or check,
    the DU/DF-vs-U prefix guard, and the real-money confirm prompt can never drift
    between the two commands. The label parameters preserve each command's
    surface vocabulary in error/prompt text:

    * ``selector_pair_hint`` — the legacy form shown in the "provide either" error
      (e.g. ``"ACCOUNT_ID --ib-login-key <key>"`` vs
      ``"--account <ib-account> --ib-login-key <key>"``).
    * ``account_flag_label`` — how the account argument is named in the
      ``--ib-login-key is required ...`` message (``"ACCOUNT_ID"`` vs
      ``"--account"``).
    * ``account_value_label`` — the prefix used when echoing the bad account value
      (``"account_id"`` vs ``"--account"``).

    When the broker-account selector is supplied the raw account string may be
    omitted and the server is the authority on the contract, so the prefix guard
    is skipped. Identity-bearing fields are trimmed before submission (the backend
    hashes account_id + ib_login_key into the deployment identity_signature and
    routes by the exact ib_login_key string; whitespace creates a distinct
    identity row and misses gateway routes)."""
    trimmed_broker_account_id = broker_account_id.strip()
    trimmed_account = account.strip()
    if not trimmed_broker_account_id and not trimmed_account:
        _fail(f"Provide either --broker-account-id <uuid> OR {selector_pair_hint}.")
    if not trimmed_broker_account_id:
        # Legacy pair form — --ib-login-key is required alongside the account.
        if not ib_login_key.strip():
            _fail(f"--ib-login-key is required when deploying with {account_flag_label}.")
        # Mirror the UI's account-prefix guard so a CLI caller can't paste an
        # account_id that contradicts --paper / --no-paper. Without this,
        # <account U...> --paper would post paper_trading=true and the supervisor
        # would create+reject the deployment row, leaving a collision-prone
        # (revision_id, account_id) entry that needs manual archive. Backend
        # `ib_port_validator.IB_PAPER_PREFIXES = ("DU", "DF")`.
        is_paper_prefix = trimmed_account.startswith(("DU", "DF"))
        if paper and not is_paper_prefix:
            _fail(
                f"{account_value_label} '{trimmed_account}' is not a paper-prefix account "
                "(expected DU* or DF*). Pass --no-paper for real-money accounts."
            )
        if not paper and (is_paper_prefix or not trimmed_account.startswith("U")):
            _fail(
                f"{account_value_label} '{trimmed_account}' is not a live-prefix account "
                "(expected U*, NOT DU/DF). Remove --no-paper for paper accounts."
            )
    if not paper:
        # The real-money confirmation MUST name what will actually be deployed.
        # The payload below prefers broker_account_id (the legacy strings are
        # dropped when it is set), so the prompt must prefer it too — otherwise a
        # caller passing BOTH --broker-account-id and a legacy --account would
        # confirm real-money against the legacy account label while the API deploys
        # the account resolved from the UUID (Codex review: misleading real-money
        # confirmation). broker_account_id wins in both the prompt and the payload.
        confirm_target = (
            f"broker account {trimmed_broker_account_id}"
            if trimmed_broker_account_id
            else trimmed_account
        )
        typer.confirm(
            f"This will start REAL-MONEY trading on {confirm_target}. Continue?",
            abort=True,
        )
    if trimmed_broker_account_id:
        return {"broker_account_id": trimmed_broker_account_id}
    return {"account_id": trimmed_account, "ib_login_key": ib_login_key.strip()}


def _api_call(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Make an authenticated request against the MSAI API.

    Fails the CLI with a clear error message on connection failure,
    request timeout, generic request errors, or non-2xx response.
    Callers that want to inspect a specific status should catch
    :class:`typer.Exit` and re-raise.
    """
    url = f"{_api_base()}{path}"
    headers = _api_headers()
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = httpx.request(
            method,
            url,
            json=json_body,
            params=params,
            headers=headers,
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

    service = DataIngestionService(
        ParquetStore(
            str(settings.parquet_root),
            partition_index_refresh=make_refresh_callback(database_url=settings.database_url),
        )
    )
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
    store = ParquetStore(
        str(settings.parquet_root),
        partition_index_refresh=make_refresh_callback(database_url=settings.database_url),
    )
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
    service = DataIngestionService(
        ParquetStore(
            str(settings.parquet_root),
            partition_index_refresh=make_refresh_callback(database_url=settings.database_url),
        )
    )
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
    account_id: str = typer.Argument(
        "",
        help="IB account id (e.g. DU1234567). Legacy form; omit when --broker-account-id is set.",
    ),
    broker_account_id: str = typer.Option(
        "",
        "--broker-account-id",
        help=(
            "Managed broker-account UUID (Task 5/6 selector). When given, the "
            "server resolves the IB account + login; the positional ACCOUNT_ID "
            "and --ib-login-key may be omitted."
        ),
    ),
    ib_login_key: str = typer.Option(
        "",
        "--ib-login-key",
        help="IB login username — required for the legacy ACCOUNT_ID form (Bug #1 trio).",
    ),
    paper: bool = typer.Option(True, help="Paper trading mode (default: True)"),
    idempotency_key: str = typer.Option(
        "",
        "--idempotency-key",
        help=(
            "Stable Idempotency-Key header value. Pass the same key when "
            "retrying after a timeout / lost response to hit the Redis "
            "reservation. Default: fresh uuid4 per invocation (so a normal "
            "stop+restart with identical identity is treated as a new "
            "request, not a cached replay of the previous deploy)."
        ),
    ),
) -> None:
    """Deploy a portfolio revision to live/paper trading.

    Codex code-review iter-3 P2: this older command predates the safety
    trio (PRs #64/#65/#66). It now mirrors `start-portfolio`'s contract —
    required `--ib-login-key`, `--no-paper` confirmation prompt, and a
    startup-safe timeout (90s > backend's 60s START_POLL_TIMEOUT_S).
    """
    # Task 5/6 either/or contract (mirrors start-portfolio). Pass
    # --broker-account-id <uuid> (server resolves account + login) OR the
    # legacy positional ACCOUNT_ID + --ib-login-key pair. When the selector
    # is supplied, the prefix guard below MUST NOT run — the operator may
    # legitimately omit the raw account string and the server is the authority
    # on the either/or contract.
    # Task 5/6 either/or contract (mirrors start-portfolio): pass
    # --broker-account-id <uuid> (server resolves account + login) OR the legacy
    # positional ACCOUNT_ID + --ib-login-key pair. The shared helper does the
    # either/or check, the DU/DF-vs-U prefix guard (skipped under the selector),
    # the --no-paper real-money confirm, and returns the trimmed account-bearing
    # payload fields.
    payload: dict[str, object] = {
        "portfolio_revision_id": portfolio_revision_id,
        "paper_trading": paper,
    }
    payload.update(
        _resolve_cli_account_payload(
            broker_account_id=broker_account_id,
            account=account_id,
            ib_login_key=ib_login_key,
            paper=paper,
            selector_pair_hint="ACCOUNT_ID --ib-login-key <key>",
            account_flag_label="ACCOUNT_ID",
            account_value_label="account_id",
        )
    )
    # Codex iter-7 P2 + PR #67 review: send Idempotency-Key so timeout /
    # network retries within the same operator action hit the Redis
    # reservation (operator passes the same --idempotency-key on retry).
    # Default to a fresh uuid4 per invocation — PR #67 review P1 caught
    # that a deterministic-from-identity key would break the legit
    # stop+restart flow (cached 24h success replays instead of a new
    # deploy). For genuine retries, operator must pass --idempotency-key
    # explicitly (same surface as start-portfolio).
    ikey = idempotency_key or uuid.uuid4().hex
    response = _api_call(
        "POST",
        "/api/v1/live/start-portfolio",
        json_body=payload,
        extra_headers={"Idempotency-Key": ikey},
        timeout=90.0,
    )
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


def _fmt_age_s(age: float | None) -> str:
    """Render a heartbeat / router age in seconds as a compact ``N.Ns``
    string, or ``stale`` when the age is unknown (``None`` — key absent /
    expired / no heartbeat). PR 2 T8."""
    if age is None:
        return "stale"
    return f"{age:.1f}s"


@live_app.command("status")
def live_status() -> None:
    """Show all active deployments + risk-halt state + per-account
    restart-authority health (PR 2 T8)."""
    response = _api_call("GET", "/api/v1/live/status", timeout=10.0)
    data = response.json()
    typer.echo(f"Risk halted: {data['risk_halted']}")
    typer.echo(f"Active nodes: {data['active_count']}")
    # PR 2 T8 — supervisor (router) liveness. The single live-supervisor is a
    # SPOF; a stale/absent router heartbeat means nothing is reaping or
    # auto-restarting crashed nodes — the fleet is unmonitored.
    #
    # PR 2 F4 — the router heartbeat key has a 90s TTL, so ``router_age`` stays
    # numeric for up to 90s after the supervisor dies. But the backend treats
    # the supervisor as DEAD far earlier: the SPOF alert fires at
    # ``ROUTER_HEARTBEAT_SPOF_THRESHOLD_S`` (30s) and the /start-portfolio gate
    # at 15s. A null-only check would render "alive (45.0s ago)" in the 30-90s
    # window, hiding an unmonitored fleet. Mirror the SPOF threshold (the exact
    # age at which the fleet alert pages) so the CLI and dashboard agree on
    # "dead" with the backend liveness semantics.
    from msai.services.fleet_alerts import ROUTER_HEARTBEAT_SPOF_THRESHOLD_S

    router_age = data.get("router_heartbeat_age_s")
    if router_age is None:
        router_health = "DOWN (no heartbeat)"
    elif router_age > ROUTER_HEARTBEAT_SPOF_THRESHOLD_S:
        router_health = (
            f"STALE ({_fmt_age_s(router_age)} ago — "
            f"exceeds {int(ROUTER_HEARTBEAT_SPOF_THRESHOLD_S)}s SPOF threshold; "
            "fleet unmonitored)"
        )
    else:
        router_health = f"alive ({_fmt_age_s(router_age)} ago)"
    typer.echo(f"Supervisor (router): {router_health}")
    typer.echo(f"Deployments ({len(data['deployments'])}):")
    for d in data["deployments"]:
        mode = "PAPER" if d["paper_trading"] else "LIVE"
        # PR 1 T14 — account context for the fleet topology. Each line names
        # the IB login, the specific account_id, and the deterministic
        # ibg_client_id so operators can correlate logs across the fleet
        # without opening the UI.
        account = d.get("account_id") or "-"
        login = d.get("ib_login_key") or "-"
        client_id = d.get("ibg_client_id")
        client_id_s = str(client_id) if client_id is not None else "-"
        typer.echo(
            f"  [{mode}] {d['id']}  status={d['status']}  "
            f"account={account}  login={login}  ibg_client_id={client_id_s}  "
            f"instruments={d['instruments']}"
        )
        # PR 2 T8 — per-account restart-authority health line. Flags a tripped
        # restart ceiling (auto_restart_paused) + the consecutive-failure
        # count + heartbeat age + halt-latch state so the operator can spot an
        # account that needs intervention from the shell.
        paused = d.get("auto_restart_paused")
        if paused:
            reason = d.get("auto_restart_pause_reason") or "unspecified"
            restart_state = f"PAUSED ({reason})"
        else:
            restart_state = "auto-restart on"
        halts = []
        if d.get("fleet_halted"):
            halts.append("FLEET")
        if d.get("account_halted"):
            halts.append("ACCOUNT")
        halt_state = "+".join(halts) if halts else "none"
        failures = d.get("consecutive_respawn_failures")
        failures_s = "-" if failures is None else str(failures)
        typer.echo(
            f"        restart={restart_state}  "
            f"consecutive_failures={failures_s}  "
            f"heartbeat={_fmt_age_s(d.get('last_heartbeat_age_s'))} ago  "
            f"halt={halt_state}"
        )
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


# ----------------------------------------------------------------------
# live: portfolio compose + deploy + observability (T10)
# Thin shims over the /api/v1/live-portfolios/* + /api/v1/live/* APIs
# hardened by PRs #64/#65/#66 (binding-fingerprint + flatness trio).
# ----------------------------------------------------------------------


def _load_config_arg(raw: str) -> dict[str, Any]:
    """Parse a ``--config`` value.

    ``@/path/to/file.json`` loads JSON from the file.
    Anything else is parsed as a literal JSON string.
    """
    if raw.startswith("@"):
        path = raw[1:]
        try:
            with open(path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            _fail(f"config file not found: {path}")
        except json.JSONDecodeError as exc:
            _fail(f"invalid JSON in {path}: {exc}")
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _fail(f"invalid JSON config: {exc}")
    if not isinstance(payload, dict):
        _fail("config must be a JSON object")
    return dict(payload)


@live_app.command("portfolio-create")
def live_portfolio_create(
    name: str = typer.Option(..., "--name", help="Portfolio display name"),
    description: str = typer.Option("", "--description", help="Optional description"),
) -> None:
    """Create a new (draft) live portfolio."""
    payload: dict[str, Any] = {"name": name, "description": description}
    # Codex code-review P1: FastAPI route is registered at /api/v1/live-portfolios
    # (no trailing slash); /api/v1/live-portfolios/ triggers a 307 redirect that
    # httpx does not follow by default, so the create silently fails.
    response = _api_call("POST", "/api/v1/live-portfolios", json_body=payload)
    _emit_json(response.json())


@live_app.command("portfolio-add-strategy")
def live_portfolio_add_strategy(
    portfolio_id: str = typer.Argument(..., help="Portfolio UUID"),
    strategy_id: str = typer.Option(..., "--strategy-id", help="Strategy UUID"),
    config: str = typer.Option(
        ...,
        "--config",
        help="Strategy config: literal JSON string OR '@/path/to/file.json'",
    ),
    instruments: str = typer.Option(
        ...,
        "--instruments",
        help="Comma-separated instrument IDs (e.g. AAPL.NASDAQ,MSFT.NASDAQ)",
    ),
    weight: str = typer.Option("1.0", "--weight", help="Capital weight (Decimal string)"),
) -> None:
    """Attach a strategy to a draft portfolio."""
    cfg = _load_config_arg(config)
    instrument_list = [s.strip() for s in instruments.split(",") if s.strip()]
    if not instrument_list:
        _fail("at least one instrument required")
    payload: dict[str, Any] = {
        "strategy_id": strategy_id,
        "config": cfg,
        "instruments": instrument_list,
        "weight": weight,
    }
    response = _api_call(
        "POST",
        f"/api/v1/live-portfolios/{_url_id(portfolio_id)}/strategies",
        json_body=payload,
    )
    _emit_json(response.json())


@live_app.command("portfolio-snapshot")
def live_portfolio_snapshot(
    portfolio_id: str = typer.Argument(..., help="Portfolio UUID"),
) -> None:
    """Freeze the draft into an immutable portfolio revision."""
    response = _api_call("POST", f"/api/v1/live-portfolios/{_url_id(portfolio_id)}/snapshot")
    _emit_json(response.json())


@live_app.command("portfolio-members")
def live_portfolio_members(
    revision_id: str = typer.Argument(..., help="Portfolio revision UUID"),
) -> None:
    """List frozen members of a portfolio revision."""
    response = _api_call(
        "GET",
        f"/api/v1/live-portfolio-revisions/{_url_id(revision_id)}/members",
    )
    _emit_json(response.json())


@live_app.command("start-portfolio")
def live_start_portfolio(
    revision: str = typer.Option(..., "--revision", help="Portfolio revision UUID"),
    broker_account_id: str = typer.Option(
        "",
        "--broker-account-id",
        help=(
            "Managed broker-account UUID (Task 5/6 selector). When given, the "
            "server resolves the IB account + login from the stored record, so "
            "--account / --ib-login-key may be omitted."
        ),
    ),
    account: str = typer.Option(
        "",
        "--account",
        help="IB account id (e.g. DUP733213). Legacy form; omit when --broker-account-id is set.",
    ),
    ib_login_key: str = typer.Option(
        "",
        "--ib-login-key",
        help="IB login username key (e.g. marin1016test). Legacy form; pair with --account.",
    ),
    paper: bool = typer.Option(
        True,
        "--paper/--no-paper",
        help="Paper trading (default). --no-paper triggers a real-money confirm.",
    ),
    idempotency_key: str = typer.Option(
        "",
        "--idempotency-key",
        help="Idempotency key (auto-generated if omitted).",
    ),
) -> None:
    """Deploy a frozen portfolio revision to live/paper trading.

    Hits ``POST /api/v1/live/start-portfolio``.  On ``--no-paper`` prompts
    for confirmation BEFORE any HTTP call.  Operators must export
    ``MSAI_API_URL=http://localhost:8800`` + ``MSAI_API_KEY=msai-dev-key``
    when running outside the backend container (dev compose exposes the
    backend at host port 8800, not the CLI default 8000).

    Two mutually-exclusive identity forms (Task 5/6 either/or contract):
    pass ``--broker-account-id <uuid>`` (server resolves account + login from
    the managed record), OR the legacy ``--account ... --ib-login-key ...``
    pair. The server is the authority on the either/or contract — the CLI
    relaxes its client-side prefix guard when the selector is supplied so a
    selector-only deploy is not rejected before the API sees it.
    """
    # Either/or contract (Task 5/6), client-side. The shared helper does the
    # either/or check, the DU/DF-vs-U prefix guard (skipped under the selector,
    # where the server is the authority), the --no-paper real-money confirm, and
    # returns the trimmed account-bearing payload fields.
    payload: dict[str, Any] = {
        "portfolio_revision_id": revision,
        "paper_trading": paper,
    }
    payload.update(
        _resolve_cli_account_payload(
            broker_account_id=broker_account_id,
            account=account,
            ib_login_key=ib_login_key,
            paper=paper,
            selector_pair_hint="--account <ib-account> --ib-login-key <key>",
            account_flag_label="--account",
            account_value_label="--account",
        )
    )
    ikey = idempotency_key or uuid.uuid4().hex
    # Codex code-review P1: send Idempotency-Key as HTTP header (backend reads
    # it via `Header(default=None, alias="Idempotency-Key")` in /start-portfolio).
    # Embedding it in the JSON body silently bypasses the Redis reservation layer
    # because PortfolioStartRequest doesn't define an `idempotency_key` field.
    # Codex iter-3 P2: timeout must exceed backend's START_POLL_TIMEOUT_S
    # (60s) — cold supervisor spawns can legitimately run that long while
    # the supervisor reaches ready/failed. 30s default would surface as
    # a CLI timeout even when the deploy is still progressing.
    response = _api_call(
        "POST",
        "/api/v1/live/start-portfolio",
        json_body=payload,
        extra_headers={"Idempotency-Key": ikey},
        timeout=90.0,
    )
    _emit_json(response.json())


@live_app.command("resume")
def live_resume() -> None:
    """Clear the persistent risk-halt flag after a kill-all."""
    response = _api_call("POST", "/api/v1/live/resume")
    _emit_json(response.json())


@live_app.command("positions")
def live_positions() -> None:
    """List open positions across all active deployments.

    The backend endpoint does not filter by deployment — operators who
    want per-deployment slices should grep the JSON output.
    """
    response = _api_call("GET", "/api/v1/live/positions")
    _emit_json(response.json())


@live_app.command("trades")
def live_trades(
    deployment: str = typer.Option("", "--deployment", help="Filter to a single deployment UUID"),
    limit: int = typer.Option(100, "--limit", help="Max rows to return"),
) -> None:
    """List recent executed trades, optionally filtered by deployment."""
    params: dict[str, Any] = {"limit": limit}
    if deployment:
        params["deployment_id"] = deployment
    response = _api_call("GET", "/api/v1/live/trades", params=params)
    _emit_json(response.json())


@live_app.command("audits")
def live_audits(
    deployment_id: str = typer.Argument(..., help="Deployment UUID"),
) -> None:
    """Show the audit-event log for one deployment."""
    response = _api_call("GET", f"/api/v1/live/audits/{_url_id(deployment_id)}")
    _emit_json(response.json())


# ======================================================================
# graduation sub-app
# ======================================================================


@graduation_app.command("list")
def graduation_list(
    stage: str = typer.Option(
        "",
        help=(
            "Filter by stage (discovery / validation / paper_candidate / "
            "paper_running / paper_review / live_candidate / live_running / "
            "paused / archived). See services.graduation.VALID_TRANSITIONS."
        ),
    ),
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
    mode: str = typer.Option(
        "quick",
        "--mode",
        help=(
            "Backtest mode: 'quick' (single-shot) or 'full' (walk-forward + Optuna). "
            "Defaults to 'quick'; passes through to the API unchanged."
        ),
    ),
    n_trials: int = typer.Option(
        0,
        "--n-trials",
        help=(
            "Full-mode trial-count override (1-1000). Ignored unless --mode=full; "
            "0 (the default) sends no override and the backend uses its configured "
            "default (~100 trials)."
        ),
    ),
) -> None:
    """Trigger a portfolio-level backtest run.

    ``--mode`` selects Quick (default) vs. Full backtest semantics on the
    server. ``--n-trials`` caps the Full-mode Optuna search (1-1000); it is
    only forwarded when ``--mode=full`` and non-zero so smoke-test callers
    can run a fast Full pass without bumping the per-portfolio default.
    """
    mode_normalized = mode.strip().lower()
    if mode_normalized not in {"quick", "full"}:
        _fail(f"--mode must be 'quick' or 'full' (got {mode!r})")
    payload: dict[str, object] = {"start_date": start, "end_date": end}
    if max_parallelism > 0:
        payload["max_parallelism"] = max_parallelism
    # Always send the requested mode explicitly. Omitting it makes the
    # server inherit ``Portfolio.default_mode``, so a portfolio whose
    # default is ``full`` would launch the Optuna walk-forward optimizer
    # even when the operator typed ``--mode quick``. Codex bot iter-5 P2
    # on PR #73 caught the silent escalation. ``--n-trials`` still only
    # applies to Full mode.
    payload["mode"] = mode_normalized
    if mode_normalized == "full" and n_trials > 0:
        payload["n_trials"] = n_trials
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
# broker sub-app — broker-account CRUD over /api/v1/broker-accounts
#
# Credential discipline (Codex iter-1 P0#2): the TWS password is NEVER a
# CLI flag (argv leaks via shell history + `ps`).  It is read from
# ``$MSAI_BROKER_TWS_PASSWORD`` (scriptable / E2E) or, if unset and stdin
# is a TTY, an interactive ``getpass.getpass()`` prompt.  The password is
# sent in the POST body to the API and is NEVER echoed to stdout.
# ======================================================================

_TWS_PASSWORD_ENV = "MSAI_BROKER_TWS_PASSWORD"


def _read_tws_password() -> str:
    """Resolve the TWS password without ever putting it on argv.

    Order: ``$MSAI_BROKER_TWS_PASSWORD`` first (scripting / E2E), then an
    interactive ``getpass`` prompt when stdin is a TTY.  Non-interactive
    callers with no env var get a clear error rather than a hang.
    """
    password = os.environ.get(_TWS_PASSWORD_ENV)
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass("TWS password: ")
    _fail(
        f"TWS password required — set ${_TWS_PASSWORD_ENV} "
        "or run interactively (stdin must be a TTY for a prompt)."
    )
    raise AssertionError("unreachable")  # _fail raises; satisfies the type checker


@broker_app.command("add")
def broker_add(
    ib_account_id: str = typer.Option(..., "--ib-account-id", help="IB account id (e.g. DU123456)"),
    ib_login_key: str = typer.Option(..., "--ib-login-key", help="Logical IB login key"),
    tws_userid: str = typer.Option(..., "--tws-userid", help="TWS / IB Gateway username"),
    trading_mode: str = typer.Option("paper", "--trading-mode", help="paper or live"),
    label: str | None = typer.Option(None, "--label", help="Optional human-readable label"),
    gateway_slot: str | None = typer.Option(
        None, "--gateway-slot", help="Pinned gateway slot (default: auto-allocate)"
    ),
) -> None:
    """Register a broker account.

    The TWS password is read from ``$MSAI_BROKER_TWS_PASSWORD`` or an
    interactive prompt — never from a CLI flag.
    """
    tws_password = _read_tws_password()
    payload: dict[str, Any] = {
        "ib_account_id": ib_account_id,
        "ib_login_key": ib_login_key,
        "trading_mode": trading_mode,
        "tws_userid": tws_userid,
        "tws_password": tws_password,
    }
    if label is not None:
        payload["label"] = label
    if gateway_slot is not None:
        payload["gateway_slot"] = gateway_slot
    response = _api_call("POST", "/api/v1/broker-accounts", json_body=payload)
    data = response.json()
    typer.echo(
        f"Created broker account {data['ib_account_id']} "
        f"(id: {data['id']}, status: {data['status']}, slot: {data['gateway_slot']})"
    )


def _broker_cell(value: object, width: int) -> str:
    """Left-justify ``value`` to ``width``, truncating overflow with an ellipsis so
    adjacent columns never abut (e.g. a 32-char credential version + the UUID id)."""
    s = str(value)
    if len(s) >= width:
        s = s[: width - 2] + "… "  # ellipsis + guaranteed separating space
    return f"{s:<{width}}"


@broker_app.command("list")
def broker_list() -> None:
    """List broker accounts (newest first; excludes archived)."""
    response = _api_call("GET", "/api/v1/broker-accounts")
    rows = response.json()
    if not rows:
        typer.echo("No broker accounts.")
        return
    header = f"{'IB ACCOUNT':<14}{'STATUS':<10}{'MODE':<8}{'SLOT':<14}{'CRED VER':<14}ID"
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in rows:
        typer.echo(
            f"{_broker_cell(row['ib_account_id'], 14)}"
            f"{_broker_cell(row['status'], 10)}"
            f"{_broker_cell(row['trading_mode'], 8)}"
            f"{_broker_cell(row['gateway_slot'], 14)}"
            f"{_broker_cell(row.get('credentials_secret_version') or '-', 14)}"
            f"{row['id']}"
        )


@broker_app.command("show")
def broker_show(
    account_id: str = typer.Argument(..., help="Broker account id (UUID)"),
) -> None:
    """Show one broker account (credential metadata only — never a secret)."""
    response = _api_call("GET", f"/api/v1/broker-accounts/{_url_id(account_id)}")
    _emit_json(response.json())


@broker_app.command("rotate")
def broker_rotate(
    account_id: str = typer.Argument(..., help="Broker account id (UUID)"),
    tws_userid: str = typer.Option(..., "--tws-userid", help="TWS / IB Gateway username"),
) -> None:
    """Rotate a broker account's stored credentials.

    Like ``add``, the new TWS password is read from
    ``$MSAI_BROKER_TWS_PASSWORD`` or an interactive prompt — never a flag.
    """
    tws_password = _read_tws_password()
    payload: dict[str, Any] = {"tws_userid": tws_userid, "tws_password": tws_password}
    response = _api_call(
        "POST",
        f"/api/v1/broker-accounts/{_url_id(account_id)}/rotate-credentials",
        json_body=payload,
    )
    data = response.json()
    typer.echo(
        f"Rotated credentials for {data['ib_account_id']} "
        f"(id: {data['id']}, version: {data.get('credentials_secret_version') or '-'})"
    )


@broker_app.command("archive")
def broker_archive(
    account_id: str = typer.Argument(..., help="Broker account id (UUID)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Archive (soft-delete) a broker account, freeing its slot + deleting its secret."""
    if not yes:
        typer.confirm(
            f"Archive broker account {account_id}? "
            "This frees its gateway slot and deletes the stored secret.",
            abort=True,
        )
    response = _api_call("POST", f"/api/v1/broker-accounts/{_url_id(account_id)}/archive")
    data = response.json()
    typer.echo(f"Archived broker account {data['ib_account_id']} (status: {data['status']})")


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

    def _parse_probe(
        response: httpx.Response,
        body_ok_fn: Callable[[Any], bool],
    ) -> dict[str, object]:
        """Derive ``ok`` from the response body when needed."""
        body: Any = None
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

    probes: list[tuple[str, str, Callable[[Any], bool]]] = [
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


@system_app.command("smoke-alert")
def smoke_alert_cmd(
    result_file: str = typer.Argument(
        ...,
        help="Path to JSON file with smoke run result (output of `msai backtest smoke --json`).",
    ),
) -> None:
    """Dispatch a smoke result as an alert via :class:`AlertingService`.

    Used by ``.github/workflows/smoke.yml`` — reads the JSON the
    nightly run wrote, constructs a single alert entry, and persists
    via the existing file-backed alert log.  Picks ``level="error"``
    when ``structural_problems`` is non-empty OR ``status`` is not
    ``"completed"``; otherwise ``"info"``.

    Note: ``status="completed"`` alone is NOT sufficient for PASS — the
    structural assertions (G5 key presence, trade-count floor >= 2,
    non-empty ``report_path``) must also hold.  The smoke CLI already
    writes a ``structural_problems`` list to the JSON; we honor it.

    Silent-failure iter-1 fix #2: this command MUST always dispatch an
    alert. If the result file is missing, unreadable, or contains
    corrupt/non-JSON output (e.g., a CLI traceback that landed in the
    file before ``2>${RESULT}.stderr`` was added), synthesize a minimal
    failure payload + dispatch level=error so the CI/operator sees the
    breakage instead of a silent ``raise JSONDecodeError`` that the
    workflow's ``|| echo`` would swallow into a warning. Also wrap the
    ``send_alert`` call itself so a downstream alerting failure exits
    with code 2 (workflow visible) rather than a Python traceback.
    """
    import logging
    from pathlib import Path

    from msai.services.alerting import AlertingService

    # --- read + parse the JSON, synthesizing on any failure ---
    payload: dict[str, Any]
    synth_problem: str | None = None
    try:
        raw = Path(result_file).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        synth_problem = f"smoke result file corrupt: {type(exc).__name__}: {exc}"
        payload = {
            "status": "failed",
            "structural_problems": [synth_problem],
            "metrics": None,
        }

    structural_problems = payload.get("structural_problems") or []
    is_pass = payload.get("status") == "completed" and not structural_problems

    level = "info" if is_pass else "error"
    if is_pass:
        smoke_config = (payload.get("metrics") or {}).get("smoke_config", "fast")
        title = f"Smoke PASS — {smoke_config}"
    elif synth_problem is not None:
        title = "Smoke FAIL (result file corrupt)"
    elif structural_problems:
        title = f"Smoke FAIL (structural) — {len(structural_problems)} problem(s)"
    else:
        title = "Smoke FAIL"

    body: dict[str, Any] = {
        "metrics": payload.get("metrics") or {},
        "status": payload.get("status"),
    }
    if structural_problems:
        body["structural_problems"] = structural_problems
    if payload.get("error_message"):
        body["error_message"] = payload["error_message"]
    message = json.dumps(body, sort_keys=True)

    # AlertingService backends may fail (disk full, file lock contention,
    # future remote sink). Wrap so the workflow sees a non-zero exit code
    # instead of a Python traceback — exit 2 distinguishes alerting
    # failure from a normal "smoke failed" exit 0 with level=error.
    try:
        AlertingService().send_alert(level=level, title=title, message=message)
    except Exception as exc:  # noqa: BLE001 — log any backend error then exit
        logging.error(
            "smoke_alert_dispatch_failed",
            exc_info=True,
        )
        typer.echo(f"alert dispatch FAILED: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"alert dispatched: {title}")


# ======================================================================
# instruments sub-app
# ======================================================================


# v1 closed quarterly CME E-mini set — the only futures roots whose
# third-Friday-of-March/June/September/December schedule
# ``current_quarterly_expiry`` correctly handles (see
# live_instrument_bootstrap.py). CL/GC/ZB/etc. follow different expiry
# cycles + venues; they need operator-specified exchange + expiry that
# the v1 CLI does not yet surface.
_FUT_QUARTERLY_ROOTS: frozenset[str] = frozenset({"ES", "NQ", "RTY", "YM"})


def _build_ib_contract_for_symbol(
    symbol: str,
    *,
    asset_class: str,
    today: date,
    primary_exchange: str = "NASDAQ",
) -> IBContract:
    """Build an IBContract for an operator-supplied symbol + asset class.

    Per-asset-class normalization replaces the closed-universe
    canonical_instrument_id() helper. IB's qualifier resolves the
    canonical alias (``AAPL.NASDAQ``, ``ESM6.CME``, ``EUR/USD.IDEALPRO``)
    at qualification time — the CLI doesn't pre-canonicalize.

    Args:
        symbol: Operator-facing root symbol (e.g. ``"AAPL"``, ``"ES"``,
            ``"EUR/USD"``). FX takes ``"BASE/QUOTE"`` form; STK/FUT take
            the bare root.
        asset_class: One of ``"stk"`` (equity/ETF), ``"fut"`` (CME E-mini
            quarterly futures — closed set ``{ES, NQ, RTY, YM}``),
            ``"cash"`` (forex).
        today: Used by ``"fut"`` to derive quarterly expiry via
            :func:`current_quarterly_expiry`.
        primary_exchange: STK ``primaryExchange`` for SMART routing
            disambiguation. NASDAQ-listed (AAPL/MSFT) is the default;
            ARCA-listed ETFs (SPY/VTI) need ``"ARCA"``; NYSE-listed
            stocks need ``"NYSE"``. Ignored for FUT/CASH.

    Raises:
        ValueError: ``asset_class`` is not in the supported set, OR
            ``"fut"`` symbol is not in the v1 closed quarterly set.
    """
    from nautilus_trader.adapters.interactive_brokers.common import IBContract

    from msai.services.nautilus.live_instrument_bootstrap import (
        current_quarterly_expiry,
    )

    if asset_class == "stk":
        return IBContract(
            secType="STK",
            symbol=symbol,
            exchange="SMART",
            primaryExchange=primary_exchange,
            currency="USD",
        )
    if asset_class == "fut":
        if symbol not in _FUT_QUARTERLY_ROOTS:
            raise ValueError(
                f"--asset-class fut: v1 supports the closed CME E-mini "
                f"quarterly set {sorted(_FUT_QUARTERLY_ROOTS)!r}; got "
                f"{symbol!r}. Other futures (CL/NYMEX, GC/COMEX, etc.) "
                f"need exchange + expiry overrides that v1 does not "
                f"surface — schedule a follow-up CLI flag."
            )
        return IBContract(
            secType="FUT",
            symbol=symbol,
            exchange="CME",
            lastTradeDateOrContractMonth=current_quarterly_expiry(today),
            currency="USD",
        )
    if asset_class == "cash":
        if "/" in symbol:
            base, quote_sym = symbol.split("/", 1)
        else:
            base, quote_sym = symbol, "USD"
        return IBContract(
            secType="CASH",
            symbol=base,
            exchange="IDEALPRO",
            currency=quote_sym,
        )
    raise ValueError(f"Unknown asset class {asset_class!r} — supported: stk, fut, cash.")


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
    asset_class: str = typer.Option(
        "stk",
        "--asset-class",
        help=(
            "Asset class for IB qualification: ``stk`` (equity/ETF, "
            "default), ``fut`` (CME E-mini quarterly: ES/NQ/RTY/YM), "
            "``cash`` (forex). Ignored when --provider databento."
        ),
    ),
    primary_exchange: str = typer.Option(
        "NASDAQ",
        "--primary-exchange",
        help=(
            "STK ``primaryExchange`` for SMART routing disambiguation. "
            "NASDAQ-listed (AAPL/MSFT) is the default; ARCA-listed "
            "ETFs (SPY/VTI) need ``--primary-exchange ARCA``; "
            "NYSE-listed stocks need ``--primary-exchange NYSE``. "
            "Ignored for FUT/CASH."
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
    * Live resolve via :func:`live_resolver.lookup_for_live` — for
      ``--provider interactive_brokers`` — connects a short-lived
      Nautilus IB client, qualifies each requested symbol against IB
      Gateway, upserts registry rows, then disconnects.

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
        # Per-asset-class IBContract factories — IB resolves canonical
        # aliases at qualification time. No closed-universe map.
        if asset_class not in {"stk", "fut", "cash"}:
            _fail(f"--asset-class {asset_class!r} is not supported. Use one of: stk, fut, cash.")

        from msai.services.nautilus.live_instrument_bootstrap import (
            exchange_local_today,
        )

        today = exchange_local_today()
        contracts: list[IBContract] = []
        for sym in symbol_list:
            try:
                contracts.append(
                    _build_ib_contract_for_symbol(
                        sym,
                        asset_class=asset_class,
                        today=today,
                        primary_exchange=primary_exchange,
                    )
                )
            except ValueError as exc:
                _fail(str(exc))

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
        # if anything downstream goes wrong. Written to stderr so
        # stdout stays valid JSON for piping into jq/scripts — the
        # Databento branch's _emit_json contract applies here too.
        typer.echo(
            f"Pre-warming IB registry: host={settings.ib_host} "
            f"port={settings.ib_port} "
            f"account={settings.ib_account_id.strip()} "
            f"asset_class={asset_class} "
            f"client_id={settings.ib_instrument_client_id} "
            f"connect_timeout={settings.ib_connect_timeout_seconds}s "
            f"request_timeout={settings.ib_request_timeout_seconds}s",
            err=True,
        )

        try:
            resolved = asyncio.run(_run_ib_resolve_for_live(contracts))
        except _IBGatewayUnreachableError as exc:
            _fail(str(exc))
        _emit_json({"provider": provider, "asset_class": asset_class, "resolved": resolved})
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


async def _run_ib_resolve_for_live(contracts: list[IBContract]) -> list[str]:
    """Short-lived Nautilus IB client lifecycle wrapping per-contract
    qualification + registry upsert.

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
    6. For each contract: ``qualifier.qualify_contract(contract)`` →
       extract canonical alias + venue/asset_class metadata → upsert
       via ``SecurityMaster._upsert_definition_and_alias``. Commit
       per contract so a mid-batch failure preserves earlier rows.
    7. ``try/finally`` teardown: ``await client._stop_async()``
       DIRECTLY. The public ``client.stop()`` only schedules
       ``_stop_async`` as a task; awaiting it ourselves guarantees
       the TCP disconnect completes before the process exits — a re-run
       within 60s needs no zombie ``client_id`` slot. FSM state doesn't
       matter because we're exiting immediately.
    """
    import os

    # Cap the reconnect loop BEFORE client construction — the client
    # reads this env var on first call to `_start_async`; setting it
    # AFTER construction is too late.
    os.environ.setdefault("IB_MAX_CONNECTION_ATTEMPTS", "1")

    # Import Nautilus only inside the function so the CLI module stays
    # importable on machines without the IB extras (ruff / mypy in CI).
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersInstrumentProviderConfig,
        SymbologyMethod,
    )
    from nautilus_trader.adapters.interactive_brokers.factories import (
        get_cached_ib_client,
        get_cached_interactive_brokers_instrument_provider,
    )
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import (
        LiveClock,
        MessageBus,
    )
    from nautilus_trader.model.identifiers import TraderId

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

        provider_cfg = InteractiveBrokersInstrumentProviderConfig(
            symbology_method=SymbologyMethod.IB_SIMPLIFIED,
            load_contracts=frozenset(contracts),
            cache_validity_days=1,
        )
        provider = get_cached_interactive_brokers_instrument_provider(
            client=client,
            clock=clock,
            config=provider_cfg,
        )
        qualifier = IBQualifier(provider)

        # Commit per contract so a mid-batch failure preserves the
        # already-qualified rows — idempotent re-run picks up where the
        # batch stopped. A single batched commit would roll back
        # contract 1's flushed rows when contract 2 fails.
        resolved: list[str] = []
        async with async_session_factory() as session:
            sm = SecurityMaster(qualifier=qualifier, db=session)
            for contract in contracts:
                try:
                    instrument = await qualifier.qualify_contract(contract)
                    alias_str = str(instrument.id)
                    routing_venue = instrument.id.venue.value
                    listing_venue = qualifier.listing_venue_for(instrument)
                    # Extract trading_hours from the qualifier provider's
                    # ContractDetails so first-time IB symbols seeded by
                    # ``msai instruments refresh`` carry their RTH/ETH
                    # schedule into ``instrument_definitions.trading_hours``.
                    # Without this, MarketHoursService.is_in_rth/eth fails
                    # open on NULL → bypasses intended gating until another
                    # path backfills hours. The pre-PR resolve_for_live
                    # path did this; the new CLI loop must too.
                    trading_hours = sm._trading_hours_for(canonical_id=alias_str)
                    await sm._upsert_definition_and_alias(
                        raw_symbol=instrument.raw_symbol.value,
                        listing_venue=listing_venue,
                        routing_venue=routing_venue,
                        asset_class=SecurityMaster._asset_class_for_instrument(instrument),
                        alias_string=alias_str,
                        trading_hours=trading_hours,
                    )
                    await session.commit()
                    resolved.append(alias_str)
                except Exception as exc:
                    await session.rollback()
                    if resolved:
                        # Operator visibility — without this, the CLI
                        # error obscures which symbols already landed in
                        # the registry (their per-contract commits stuck
                        # before this rollback).
                        typer.echo(
                            f"Already-qualified symbols (registry rows committed): "
                            f"{resolved!r}; failure on contract={contract!r}: {exc}",
                            err=True,
                        )
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


from enum import StrEnum  # noqa: E402 -- used only by _AssetClassChoice below


class _AssetClassChoice(StrEnum):
    """Typer-native choice constraint for --asset-class. Matches the
    registry DB taxonomy (ck_instrument_definitions_asset_class)."""

    equity = "equity"
    futures = "futures"
    fx = "fx"
    option = "option"


@instruments_app.command("bootstrap")
def instruments_bootstrap(
    provider: str = typer.Option(..., "--provider", help="Provider: 'databento'"),
    symbols: str = typer.Option(
        ...,
        "--symbols",
        help="Comma-separated symbols (e.g. AAPL,SPY,ES.n.0)",
    ),
    asset_class: _AssetClassChoice | None = typer.Option(  # noqa: B008 -- Typer pattern
        None,
        "--asset-class",
        help="Override auto-detection (registry taxonomy: equity|futures|fx|option)",
    ),
    max_concurrent: int = typer.Option(3, "--max-concurrent", min=1, max=3),
    exact_id: list[str] = typer.Option(  # noqa: B008 -- Typer pattern
        [],
        "--exact-id",
        help="Disambiguation: SYMBOL:ALIAS_STRING (repeatable). Value is a "
        "canonical alias_string from a prior ambiguity 422's candidates.",
    ),
) -> None:
    """Bootstrap equity/ETF/futures symbols into the registry via Databento.

    Registers symbols as backtest-discoverable. Does NOT qualify live IB
    instruments — run `msai instruments refresh --provider interactive_brokers`
    before live deployment.
    """
    if provider != "databento":
        _fail(
            f"Unsupported provider {provider!r} for bootstrap. Supported: databento. "
            "For IB qualification use `msai instruments refresh --provider interactive_brokers`."
        )

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        _fail("no symbols provided")

    # exact_ids value is a canonical alias_string (e.g., "BRK.B.XNYS")
    # from a prior ambiguity 422's candidates[] — NOT a numeric instrument_id.
    exact_ids: dict[str, str] = {}
    for pair in exact_id:
        sym, sep, alias_str = pair.rpartition(":")
        if not sep or not sym or not alias_str:
            _fail(f"--exact-id expects SYMBOL:ALIAS_STRING format, got {pair!r}")
        exact_ids[sym] = alias_str

    body: dict[str, Any] = {
        "provider": provider,
        "symbols": symbol_list,
        "max_concurrent": max_concurrent,
    }
    if asset_class is not None:
        body["asset_class_override"] = asset_class.value  # Enum → string for JSON
    if exact_ids:
        body["exact_ids"] = exact_ids

    # Bypass _api_call because it _fail()s on any non-2xx — bootstrap's 207
    # Multi-Status partial-success is the dominant mixed-batch case.
    url = f"{_api_base()}/api/v1/instruments/bootstrap"
    try:
        response = httpx.request(
            "POST",
            url,
            json=body,
            headers=_api_headers(),
            timeout=60.0,
        )
    except httpx.ConnectError:
        _fail(f"Connection refused — is the backend running at {_api_base()}?")
    except httpx.RequestError as exc:
        _fail(f"Request failed: {type(exc).__name__}: {exc}")

    if response.status_code not in (200, 207, 422):
        _fail(f"API error ({response.status_code}): {response.text}")

    payload = response.json()

    # Human-readable per-symbol summary on stderr.
    for item in payload.get("results", []):
        sym = item["symbol"]
        out = item["outcome"]
        msg = f"{sym} → {out}"
        if item.get("canonical_id"):
            msg += f" ({item['canonical_id']})"
        if item.get("diagnostics"):
            msg += f" [{item['diagnostics']}]"
        typer.echo(msg, err=True)

    summary = payload.get("summary", {})
    typer.echo(
        f"\nSummary: {summary.get('total', 0)} total · "
        f"{summary.get('created', 0)} created · "
        f"{summary.get('noop', 0)} noop · "
        f"{summary.get('alias_rotated', 0)} rotated · "
        f"{summary.get('failed', 0)} failed",
        err=True,
    )

    # Structured JSON on stdout (house style matches `_emit_json`).
    _emit_json(payload)

    if summary.get("failed", 0) > 0 or response.status_code != 200:
        raise typer.Exit(code=1)


# ======================================================================
# T1: alerts sub-app
# ======================================================================


@alerts_app.command("list")
def alerts_list(
    limit: int = typer.Option(50, "--limit", help="Max rows (server clamps to [1, 200])"),
) -> None:
    """List recent operational alerts (envelope: ``{alerts: [...]}``)."""
    response = _api_call("GET", "/api/v1/alerts/", params={"limit": limit})
    _emit_json(response.json())


# ======================================================================
# T2: strategy edit + delete (modifies strategy_app)
# ======================================================================


@strategy_app.command("edit")
def strategy_edit(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
    description: str | None = typer.Option(
        None,
        "--description",
        help=(
            "New description (omit to leave unchanged; pass an empty string "
            "to clear). Codex code-review iter-1 P2 distinguished omission "
            "from empty value — truthiness would have dropped --description ''."
        ),
    ),
    default_config: str | None = typer.Option(
        None,
        "--default-config",
        help=(
            "Replacement default_config: literal JSON object OR '@/path/to/file.json'. "
            "Omit to leave unchanged."
        ),
    ),
) -> None:
    """Update a strategy's description and/or default_config (PATCH)."""
    payload: dict[str, Any] = {}
    if description is not None:
        payload["description"] = description
    if default_config is not None:
        payload["default_config"] = _load_config_arg(default_config)
    if not payload:
        _fail("provide at least one of --description or --default-config")
    response = _api_call(
        "PATCH",
        f"/api/v1/strategies/{_url_id(strategy_id)}",
        json_body=payload,
    )
    _emit_json(response.json())


@strategy_app.command("delete")
def strategy_delete(
    strategy_id: str = typer.Argument(..., help="Strategy UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a strategy registry row (returns 200 MessageResponse)."""
    if not yes:
        typer.confirm(f"Delete strategy {strategy_id}?", abort=True)
    response = _api_call("DELETE", f"/api/v1/strategies/{_url_id(strategy_id)}")
    _emit_json(response.json())


# ======================================================================
# T3: graduation create + stage (modifies graduation_app)
# ======================================================================


@graduation_app.command("create")
def graduation_create(
    strategy_id: str = typer.Option(..., "--strategy-id", help="Strategy UUID"),
    config: str = typer.Option(
        ...,
        "--config",
        help="Strategy config: literal JSON object OR '@/path/to/file.json'",
    ),
    metrics: str = typer.Option(
        "",
        "--metrics",
        help=(
            "Metrics JSON: literal object OR '@/path/to/file.json'. "
            "Server requires metrics; defaults to {} if omitted."
        ),
    ),
    research_job_id: str = typer.Option(
        "",
        "--research-job-id",
        help="Optional research_job UUID this candidate derives from",
    ),
    notes: str = typer.Option("", "--notes", help="Optional notes"),
) -> None:
    """Create a new graduation candidate (stage server-set to ``discovery``)."""
    cfg = _load_config_arg(config)
    metrics_obj: dict[str, Any] = _load_config_arg(metrics) if metrics else {}
    payload: dict[str, Any] = {
        "strategy_id": strategy_id,
        "config": cfg,
        "metrics": metrics_obj,
    }
    if research_job_id:
        payload["research_job_id"] = research_job_id
    if notes:
        payload["notes"] = notes
    response = _api_call(
        "POST",
        "/api/v1/graduation/candidates",
        json_body=payload,
    )
    _emit_json(response.json())


@graduation_app.command("stage")
def graduation_stage(
    candidate_id: str = typer.Argument(..., help="Graduation candidate UUID"),
    stage: str = typer.Option(..., "--stage", help="Target stage"),
    reason: str = typer.Option("", "--reason", help="Optional reason for the transition"),
) -> None:
    """Advance a candidate to ``--stage`` with an optional reason."""
    payload: dict[str, Any] = {"stage": stage}
    if reason:
        payload["reason"] = reason
    response = _api_call(
        "POST",
        f"/api/v1/graduation/candidates/{_url_id(candidate_id)}/stage",
        json_body=payload,
    )
    _emit_json(response.json())


# ======================================================================
# T4: research sweep + walk-forward + promote (modifies research_app)
# ======================================================================


@research_app.command("sweep")
def research_sweep(
    config: str = typer.Option(
        ...,
        "--config",
        help=(
            "Sweep payload: literal JSON object OR '@/path/to/file.json'. "
            "The file IS the full POST body (flat, not wrapped in {strategy_id, config})."
        ),
    ),
) -> None:
    """Launch a parameter sweep research job."""
    payload = _load_config_arg(config)
    response = _api_call("POST", "/api/v1/research/sweeps", json_body=payload)
    _emit_json(response.json())


@research_app.command("walk-forward")
def research_walk_forward(
    config: str = typer.Option(
        ...,
        "--config",
        help=(
            "Walk-forward payload: literal JSON object OR '@/path/to/file.json'. "
            "The file IS the full POST body (must include train_days + test_days)."
        ),
    ),
) -> None:
    """Launch a walk-forward optimisation research job."""
    payload = _load_config_arg(config)
    response = _api_call("POST", "/api/v1/research/walk-forward", json_body=payload)
    _emit_json(response.json())


@research_app.command("promote")
def research_promote(
    job_id: str = typer.Option(..., "--job-id", help="Completed research_job UUID"),
    trial_index: int = typer.Option(
        -1,
        "--trial-index",
        help="Specific trial index to promote (default: server picks best)",
    ),
    notes: str = typer.Option("", "--notes", help="Optional notes"),
) -> None:
    """Promote a completed research job's result to a graduation candidate."""
    payload: dict[str, Any] = {"research_job_id": job_id}
    if trial_index >= 0:
        payload["trial_index"] = trial_index
    if notes:
        payload["notes"] = notes
    response = _api_call("POST", "/api/v1/research/promotions", json_body=payload)
    _emit_json(response.json())


# ======================================================================
# T5: backtest report + trades (modifies backtest_app)
# ======================================================================


@backtest_app.command("report")
def backtest_report(
    backtest_id: str = typer.Argument(..., help="Backtest UUID"),
    out: str = typer.Option("", "--out", help="Output HTML file path (default: stdout)"),
) -> None:
    """Download a backtest's QuantStats HTML report (two-step token flow)."""
    safe_id = _url_id(backtest_id)
    # Step 1: mint signed URL.
    token_response = _api_call(
        "POST",
        f"/api/v1/backtests/{safe_id}/report-token",
        timeout=60.0,
    )
    token_payload = token_response.json()
    signed_url = token_payload.get("signed_url")
    if not isinstance(signed_url, str) or not signed_url:
        _fail("report-token response missing signed_url")
    # Step 2: GET the signed URL — response is text/html.
    html_response = _api_call("GET", signed_url, timeout=60.0)
    html = html_response.text
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
        typer.echo(f"Report written to {out}", err=True)
    else:
        typer.echo(html)


@backtest_app.command("trades")
def backtest_trades(
    backtest_id: str = typer.Argument(..., help="Backtest UUID"),
    page: int = typer.Option(1, "--page", help="Page number (1-indexed)"),
    page_size: int = typer.Option(
        100,
        "--page-size",
        help="Page size (server clamps to max 500)",
    ),
    all_pages: bool = typer.Option(
        False,
        "--all",
        help="Loop through all pages and emit a merged ``items`` list",
    ),
    out: str = typer.Option("", "--out", help="Output JSON file path (default: stdout)"),
) -> None:
    """Fetch paginated trades for a backtest."""
    safe_id = _url_id(backtest_id)
    if not all_pages:
        response = _api_call(
            "GET",
            f"/api/v1/backtests/{safe_id}/trades",
            params={"page": page, "page_size": page_size},
        )
        payload = response.json()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            typer.echo(f"Trades written to {out}", err=True)
        else:
            _emit_json(payload)
        return
    # --all: iterate pages, terminate when returned rows < server's page_size.
    # Use the response's page_size (server may have clamped down to 500).
    aggregated: list[Any] = []
    current_page = 1
    total: int | None = None
    while True:
        response = _api_call(
            "GET",
            f"/api/v1/backtests/{safe_id}/trades",
            params={"page": current_page, "page_size": page_size},
        )
        page_payload = response.json()
        items = page_payload.get("items", [])
        aggregated.extend(items)
        if total is None:
            total = page_payload.get("total")
        server_page_size = page_payload.get("page_size", page_size)
        if len(items) < server_page_size:
            break
        current_page += 1
    merged: dict[str, Any] = {"items": aggregated, "total": total, "pages_fetched": current_page}
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, default=str)
        typer.echo(f"Trades written to {out}", err=True)
    else:
        _emit_json(merged)


# ======================================================================
# T8: backtest smoke — canonical multi-strategy portfolio smoke (AAPL+SPY)
# ======================================================================


# Required keys on the G5 metrics block when the smoke run reports
# ``status="completed"``.  Any missing key is a STRUCTURAL FAIL even
# though the run's lifecycle status is terminal-success.
_SMOKE_G5_REQUIRED: frozenset[str] = frozenset(
    {
        "total_return",
        "pnl",
        "sharpe",
        "sortino",
        "alpha",
        "beta",
        "max_drawdown",
        "trade_count_by_strategy",
        "trade_count_total",
        "benchmark_symbol",
        "smoke_config",
    }
)

# Cold-nightly budget + slack — the smoke endpoint synchronously runs
# ingest before returning, so the create-call timeout must cover the
# upstream budget (~60 min for a cold nightly window).
_SMOKE_HTTP_TIMEOUT_SECONDS: float = 3700.0

# After the create POST returns, the worker has at most this long to
# transition the run to a terminal status. Decoupled from the
# create-call timeout (silent-failure iter-1 fix #6): the previous code
# used _SMOKE_HTTP_TIMEOUT_SECONDS for BOTH the create call AND the
# poll deadline, giving a worst-case 2h block (3700s + 3700s) on a
# hung worker. By the time the create POST returns, ingest is done and
# the worker should reach terminal in well under 10 minutes.
_SMOKE_POLL_DEADLINE_SECONDS: float = 600.0


@backtest_app.command("smoke")
def smoke_cmd(
    config: str = typer.Option(
        "fast",
        "--config",
        help="Smoke config: 'fast' (1 month) or 'nightly' (2024 full year).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the smoke result as a single JSON document on stdout.",
    ),
) -> None:
    """Run the canonical multi-strategy portfolio smoke against AAPL+SPY.

    PRD: ``docs/prds/ingest-backtest-smoke-test.md`` v1.3.

    On structural FAIL (status != "completed", missing G5 keys, any
    ``smoke_market_order/<symbol>`` strategy producing <1 trade — the
    per-instrument deterministic floor — or empty ``report_path``), exits
    with code 1 and prints the problems list to stderr.  ``--json`` always
    emits a single JSON document on stdout including a
    ``structural_problems`` array.
    """
    if config not in {"fast", "nightly"}:
        typer.echo(
            f"unknown --config {config}; expected 'fast' or 'nightly'",
            err=True,
        )
        raise typer.Exit(code=2)

    def _smoke_api_call(method: str, path: str, *, stage: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """``_api_call`` that honors the --json contract on failure.

        ``_api_call`` raises ``typer.Exit`` (after printing to stderr) on any
        non-2xx / timeout — e.g. a 409 ingest-lock contention on create. In
        ``--json`` mode the command promises a single JSON document on stdout,
        so emit a structured failure payload before re-raising (Codex PR
        review P2). Non-json mode keeps ``_api_call``'s stderr behavior.
        """
        try:
            return _api_call(method, path, **kwargs)
        except typer.Exit:
            if json_output:
                typer.echo(
                    json.dumps(
                        {
                            "id": None,
                            "status": "api_error",
                            "structural_problems": [
                                f"API call failed at {stage} ({method} {path})"
                            ],
                            "metrics": None,
                        }
                    )
                )
            raise

    create_resp = _smoke_api_call(
        "POST",
        f"/api/v1/portfolios/smoke/runs?config={config}",
        stage="create",
        timeout=_SMOKE_HTTP_TIMEOUT_SECONDS,
    )
    run: dict[str, Any] = create_resp.json()
    run_id = run["id"]

    # Poll the run-detail endpoint until terminal — always do at least
    # one GET so the CLI surfaces the run-detail shape (status,
    # metrics, report_path) the structural assertions need rather than
    # the create-response body shape.  Terminal states match
    # ``PortfolioRunStatus`` (portfolio_enums.py:53) — note the
    # single-L spelling of ``canceled``.
    #
    # Poll deadline is decoupled from the create-call timeout
    # (silent-failure iter-1 fix #6). KeyboardInterrupt during the poll
    # leaves the run in-flight on the server side — surface the run id
    # so the operator can manually poll
    # ``GET /api/v1/portfolios/runs/<id>`` later instead of losing it
    # to a swallowed ^C.
    terminal = {"completed", "failed", "canceled"}
    timeout_at = time.monotonic() + _SMOKE_POLL_DEADLINE_SECONDS
    try:
        while True:
            if time.monotonic() > timeout_at:
                # In --json mode emit a failure JSON document on stdout
                # before exiting so automation parsing stdout still gets a
                # well-formed result (Codex code-review P2 — the branch
                # previously wrote only to stderr, breaking the JSON
                # contract). Human mode keeps the stderr line.
                if json_output:
                    typer.echo(
                        json.dumps(
                            {
                                "id": run_id,
                                "status": "timeout",
                                "structural_problems": [
                                    "poll deadline exceeded; run still in flight"
                                ],
                                "metrics": None,
                            }
                        )
                    )
                else:
                    typer.echo(
                        f"FAIL @ poll: timed out waiting for run {run_id} "
                        f"(deadline={_SMOKE_POLL_DEADLINE_SECONDS}s after create)",
                        err=True,
                    )
                raise typer.Exit(code=1)
            status_resp = _smoke_api_call(
                "GET",
                f"/api/v1/portfolios/runs/{_url_id(run_id)}",
                stage="poll",
            )
            run = status_resp.json()
            if run.get("status") in terminal:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        typer.echo(
            f"RAN_AS_PENDING: smoke run {run_id} was enqueued but the CLI was "
            f"interrupted before the worker reached terminal. Poll manually "
            f"with `msai portfolio runs` or "
            f"GET /api/v1/portfolios/runs/{run_id}.",
            err=True,
        )
        raise typer.Exit(code=130) from None

    metrics: dict[str, Any] = run.get("metrics") or {}
    structural_problems: list[str] = []

    if run.get("status") != "completed":
        structural_problems.append(
            f"status={run.get('status')!r}: {run.get('error_message') or 'unknown'}"
        )
    else:
        missing_keys = _SMOKE_G5_REQUIRED - set(metrics.keys())
        if missing_keys:
            structural_problems.append(f"missing G5 keys: {sorted(missing_keys)}")
        by_strategy: dict[str, Any] = metrics.get("trade_count_by_strategy") or {}
        # Per-instrument deterministic floor: each ``smoke_market_order/<symbol>``
        # strategy is designed to emit ≥1 order/instrument every run. A 0 (or an
        # ABSENT key) means that symbol's catalog had no bars at backtest time —
        # fail loudly rather than let another symbol's volume mask it (the
        # AAPL=0 / SPY=440 prod incident, which the old SUM floor passed).
        # ``ema_cross`` is NOT floored — it can legitimately be 0 in a short window.
        # ``config`` is validated to be a SmokeConfigName earlier in this command.
        for sym in SMOKE_CONFIGS[cast("SmokeConfigName", config)].symbols:
            name = f"__smoke__/smoke_market_order/{sym}"
            count = int(by_strategy.get(name, -1))
            if count < 1:
                shown = "absent" if name not in by_strategy else str(count)
                structural_problems.append(
                    f"{name} produced {shown} trades; smoke_market_order must emit "
                    "≥1 per instrument (that symbol's catalog likely had no bars)"
                )
        if not run.get("report_path"):
            structural_problems.append("report_path empty — report generation failed")

    if json_output:
        out = {**run, "structural_problems": structural_problems}
        typer.echo(json.dumps(out))
        if structural_problems:
            raise typer.Exit(code=1)
        return

    if not structural_problems:
        _print_smoke_metrics_table(metrics, run.get("report_path") or "")
        typer.echo(f"\nPASS — Portfolio run {run['id']}")
        return

    for problem in structural_problems:
        typer.echo(f"FAIL: {problem}", err=True)
    raise typer.Exit(code=1)


def _print_smoke_metrics_table(metrics: dict[str, Any], report_path: str) -> None:
    """Render the G5 metrics block as a compact human-readable table."""

    def row(label: str, value: str) -> None:
        typer.echo(f"  {label:<28} {value:>14}")

    typer.echo("Smoke metrics:")
    row("Total return", f"{(metrics.get('total_return') or 0):.2%}")
    row("P&L (USD)", f"${(metrics.get('pnl') or 0):,.0f}")
    row("Sharpe", f"{(metrics.get('sharpe') or 0):.2f}")
    row("Sortino", f"{(metrics.get('sortino') or 0):.2f}")
    row("Alpha vs SPY", f"{(metrics.get('alpha') or 0):.2%}")
    row("Beta vs SPY", f"{(metrics.get('beta') or 0):.2f}")
    row("Max drawdown", f"{(metrics.get('max_drawdown') or 0):.2%}")
    row("Trades total", str(metrics.get("trade_count_total") or 0))
    for strat, n in (metrics.get("trade_count_by_strategy") or {}).items():
        row(f"  · {strat.removeprefix('__smoke__/')}", str(n))
    row("Benchmark", str(metrics.get("benchmark_symbol") or "—"))
    row("Smoke config", str(metrics.get("smoke_config") or "—"))
    if report_path:
        typer.echo(f"\nReport: {report_path}")


# ======================================================================
# T6: live status-show + portfolio list/show/draft-members (modifies live_app)
# ======================================================================


@live_app.command("status-show")
def live_status_show(
    deployment_id: str = typer.Argument(..., help="Deployment UUID"),
) -> None:
    """Show status for a single deployment (distinct from ``live status``)."""
    response = _api_call(
        "GET",
        f"/api/v1/live/status/{_url_id(deployment_id)}",
        timeout=10.0,
    )
    _emit_json(response.json())


@live_app.command("portfolio-list")
def live_portfolio_list() -> None:
    """List all live portfolios."""
    response = _api_call("GET", "/api/v1/live-portfolios")
    _emit_json(response.json())


@live_app.command("portfolio-show")
def live_portfolio_show(
    portfolio_id: str = typer.Argument(..., help="Live portfolio UUID"),
) -> None:
    """Show one live portfolio's detail."""
    response = _api_call(
        "GET",
        f"/api/v1/live-portfolios/{_url_id(portfolio_id)}",
    )
    _emit_json(response.json())


@live_app.command("portfolio-draft-members")
def live_portfolio_draft_members(
    portfolio_id: str = typer.Argument(..., help="Live portfolio UUID"),
) -> None:
    """List DRAFT members of a portfolio (use ``portfolio-members`` for frozen revisions)."""
    response = _api_call(
        "GET",
        f"/api/v1/live-portfolios/{_url_id(portfolio_id)}/members",
    )
    _emit_json(response.json())


# ======================================================================
# T7: portfolio create + run-show + run-report (modifies portfolio_app)
# ======================================================================


@portfolio_app.command("create")
def portfolio_create(
    config: str = typer.Option(
        ...,
        "--config",
        help=(
            "Portfolio payload: literal JSON object OR '@/path/to/file.json'. "
            "Required fields: name, objective, base_capital, allocations[]."
        ),
    ),
) -> None:
    """Create a new research-backtest portfolio."""
    payload = _load_config_arg(config)
    response = _api_call("POST", "/api/v1/portfolios", json_body=payload)
    _emit_json(response.json())


@portfolio_app.command("run-show")
def portfolio_run_show(
    run_id: str = typer.Argument(..., help="Portfolio run UUID"),
) -> None:
    """Show one portfolio run's detail."""
    response = _api_call(
        "GET",
        f"/api/v1/portfolios/runs/{_url_id(run_id)}",
    )
    _emit_json(response.json())


@portfolio_app.command("run-report")
def portfolio_run_report(
    run_id: str = typer.Argument(..., help="Portfolio run UUID"),
    out: str = typer.Option("", "--out", help="Output HTML file path (default: stdout)"),
) -> None:
    """Download a portfolio run's HTML report."""
    response = _api_call(
        "GET",
        f"/api/v1/portfolios/runs/{_url_id(run_id)}/report",
        timeout=60.0,
    )
    html = response.text
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
        typer.echo(f"Report written to {out}", err=True)
    else:
        typer.echo(html)


# ======================================================================
# Portfolio-backtest CLI surface (PR companion to API+UI shipping):
#   - create-from-strategies : F1c bridge POST /portfolios (strategy_ids path)
#   - cancel                 : POST /portfolios/runs/{id}/cancel
#   - promote-to-live        : POST /portfolios/runs/{id}/promote-to-live
#
# The ``portfolio create`` command above remains the JSON-body escape
# hatch for legacy ``allocations[]`` callers and arbitrary payload shapes.
# ``create-from-strategies`` is the convenience wrapper that exposes the
# F1c strategy-first compose path with named flags so operators don't
# have to hand-roll the JSON for the common case.
# ======================================================================


@portfolio_app.command("create-from-strategies")
def portfolio_create_from_strategies(
    name: str = typer.Option(..., "--name", help="Portfolio name (max 128 chars)"),
    strategy_id: list[str] = typer.Option(
        ...,
        "--strategy-id",
        help=(
            "Strategy UUID to include in the portfolio. Pass once per "
            "strategy: --strategy-id <uuid> --strategy-id <uuid> ..."
        ),
    ),
    objective: str = typer.Option(
        "maximize_sharpe",
        "--objective",
        help=(
            "Objective: equal_weight, manual, maximize_profit, maximize_sharpe, "
            "maximize_sortino, maximize_calmar, minimize_max_drawdown."
        ),
    ),
    base_capital: float = typer.Option(
        100000.0,
        "--base-capital",
        help="Base capital in account currency (must be > 0).",
    ),
    allocator: str = typer.Option(
        "equal_weight",
        "--allocator",
        help=(
            "Weight allocator: equal_weight, inverse_vol, vol_targeted, "
            "fixed_weight (Quick mode only)."
        ),
    ),
    default_mode: str = typer.Option(
        "quick",
        "--default-mode",
        help="Default backtest mode for /runs: 'quick' or 'full'.",
    ),
    max_position_size: float = typer.Option(
        0.0,
        "--max-position-size",
        help="Safety cap: max fraction of base capital per position (0,1]. 0 = no override.",
    ),
    max_drawdown_halt: float = typer.Option(
        0.0,
        "--max-drawdown-halt",
        help=("Safety cap: drawdown threshold (0,1] that triggers a halt. 0 = no override."),
    ),
    description: str = typer.Option("", "--description", help="Optional description."),
) -> None:
    """Create a portfolio via the F1c strategy-first bridge.

    POSTs to ``/api/v1/portfolios`` with the ``strategy_ids`` shape — the
    orchestration layer auto-derives default :class:`GraduationCandidate`
    rows from each strategy roster entry (one Candidate per Strategy,
    idempotent on repeat compose).  No need to pre-graduate candidates or
    hand-roll an ``allocations[]`` payload for the common case.

    For the legacy explicit-Candidate path (custom weights, manual
    objective with per-allocation overrides), use ``portfolio create
    --config @payload.json`` instead.
    """
    mode_normalized = default_mode.strip().lower()
    if mode_normalized not in {"quick", "full"}:
        _fail(f"--default-mode must be 'quick' or 'full' (got {default_mode!r})")
    if not strategy_id:
        _fail("at least one --strategy-id is required")
    payload: dict[str, object] = {
        "name": name,
        "objective": objective,
        "base_capital": base_capital,
        "strategy_ids": strategy_id,
        "allocator_name": allocator,
        "default_mode": mode_normalized,
    }
    if description:
        payload["description"] = description
    if max_position_size > 0:
        payload["max_position_size"] = max_position_size
    if max_drawdown_halt > 0:
        payload["max_drawdown_halt"] = max_drawdown_halt
    response = _api_call("POST", "/api/v1/portfolios", json_body=payload)
    _emit_json(response.json())


@portfolio_app.command("cancel")
def portfolio_cancel(
    run_id: str = typer.Argument(..., help="Portfolio run UUID to cancel"),
) -> None:
    """Cancel a non-terminal portfolio backtest run.

    POSTs to ``/api/v1/portfolios/runs/{run_id}/cancel``. Returns 200 +
    the updated run JSON on success, 404 if the run id doesn't exist,
    or 409 if the run is already in a terminal state (completed /
    failed / canceled). The worker's status-transition guard refuses
    to lift a canceled run back to running, so the flip is safe even
    under an arq retry race.
    """
    response = _api_call(
        "POST",
        f"/api/v1/portfolios/runs/{_url_id(run_id)}/cancel",
    )
    _emit_json(response.json())


@portfolio_app.command("promote-to-live")
def portfolio_promote_to_live(
    run_id: str = typer.Argument(..., help="Portfolio run UUID (must be 'completed')"),
    account_id: str = typer.Option(
        ...,
        "--account-id",
        help=(
            "IB account id to bind to the new live portfolio. Phase 1 paper-only: "
            "must start with 'DU' (paper accounts). Real-money 'U...' ids are "
            "rejected at the API with a 422 PAPER_ONLY_ENFORCED error."
        ),
    ),
) -> None:
    """Promote a completed portfolio run to a paper live portfolio.

    POSTs to ``/api/v1/portfolios/runs/{run_id}/promote-to-live`` with
    ``{"account_id": "DU..."}``. On success emits the new
    ``live_portfolio_id`` + ``live_portfolio_revision_id`` so the
    operator can immediately POST to ``/api/v1/live/start-portfolio``
    with the revision id.

    Phase 1 enforcement:

    - run status must be ``completed`` (422 otherwise)
    - ``account_id`` must start with ``DU`` (422 otherwise — paper only)
    - the resulting composition must pass RiskEngine validation (422 otherwise)
    """
    payload: dict[str, object] = {"account_id": account_id}
    response = _api_call(
        "POST",
        f"/api/v1/portfolios/runs/{_url_id(run_id)}/promote-to-live",
        json_body=payload,
    )
    _emit_json(response.json())


# ======================================================================
# T8: market-data sub-app
# ======================================================================


@market_data_app.command("bars")
def market_data_bars(
    symbol: str = typer.Argument(..., help="Ticker symbol (e.g. AAPL)"),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
    interval: str = typer.Option("1m", "--interval", help="Bar interval (default 1m)"),
) -> None:
    """Fetch OHLCV bars for ``symbol`` over the date range."""
    response = _api_call(
        "GET",
        f"/api/v1/market-data/bars/{_url_id(symbol)}",
        params={"start": start, "end": end, "interval": interval},
    )
    _emit_json(response.json())


@market_data_app.command("symbols")
def market_data_symbols() -> None:
    """List available symbols grouped by asset class."""
    response = _api_call("GET", "/api/v1/market-data/symbols")
    _emit_json(response.json())


@market_data_app.command("status")
def market_data_status() -> None:
    """Show API-backed storage status (distinct from top-level ``data-status``)."""
    response = _api_call("GET", "/api/v1/market-data/status")
    _emit_json(response.json())


@market_data_app.command("ingest")
def market_data_ingest(
    asset_class: str = typer.Option(
        ...,
        "--asset-class",
        help="Asset class: stocks | equities | indexes | futures | options | crypto",
    ),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers"),
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
    provider: str = typer.Option(
        "auto",
        "--provider",
        help="Data provider: auto | databento | polygon",
    ),
    dataset: str = typer.Option("", "--dataset", help="Override default Databento dataset"),
    data_schema: str = typer.Option(
        "",
        "--data-schema",
        help="Override default Databento schema",
    ),
) -> None:
    """Enqueue an ingestion job via the API (distinct from top-level ``ingest``)."""
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        _fail("no symbols provided")
    body: dict[str, Any] = {
        "asset_class": asset_class,
        "symbols": symbol_list,
        "start": start,
        "end": end,
        "provider": provider,
    }
    if dataset:
        body["dataset"] = dataset
    if data_schema:
        body["data_schema"] = data_schema
    response = _api_call("POST", "/api/v1/market-data/ingest", json_body=body)
    _emit_json(response.json())


# ======================================================================
# T10: auth sub-app (+ top-level whoami alias)
# ======================================================================


@auth_app.command("me")
def auth_me() -> None:
    """Show the current authenticated user (JWT or X-API-Key)."""
    response = _api_call("GET", "/api/v1/auth/me")
    _emit_json(response.json())


@auth_app.command("logout")
def auth_logout() -> None:
    """Log the current session out (server returns 200 MessageResponse)."""
    response = _api_call("POST", "/api/v1/auth/logout")
    _emit_json(response.json())


@app.command("whoami")
def whoami() -> None:
    """Alias for ``msai auth me``."""
    response = _api_call("GET", "/api/v1/auth/me")
    _emit_json(response.json())


# Register the symbols sub-app at module load time (after defining _api_call).
# Import here to avoid circular dependency.
from msai.cli_symbols import app as symbols_app  # noqa: E402

app.add_typer(symbols_app, name="symbols")


if __name__ == "__main__":
    app()
