"""Integration tests for ``msai system gateway-watchdog-tick`` (Task 2).

These drive the CLI tick through the real decision logic (Task 1) with:

- a real Postgres testcontainer for the live-active ``LiveDeployment`` count
  (reusing the module-scoped ``_broker_migrated_url`` from ``conftest.py``),
- an in-memory fake Redis at the ``get_redis_pool`` seam (the watchdog uses
  only ``get``/``set``/``delete``/``aclose``), and
- a stub ``AlertService.send_alert`` to assert the alert level without SMTP.

The actionable-down cases are driven through the ``--inject-health down``
dry-run path so the test isn't fighting the grace window / the
``down_since``-clear-on-RESTART (per the plan Task 2 Step 1 guidance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

import msai.cli as cli
from msai.cli import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeRedis:
    """Minimal async Redis stand-in backed by a shared dict.

    Supports the watchdog's surface: ``get``/``set``/``delete``/``aclose``.
    The backing store is shared across pool instances so that successive
    CLI invocations (each open their own pool) see persisted state — exactly
    like a real Redis between ticks.
    """

    def __init__(self, store: dict[str, str], *, fail_on_get: bool = False) -> None:
        self._store = store
        self._fail_on_get = fail_on_get

    async def get(self, key: str) -> str | None:
        if self._fail_on_get:
            raise ConnectionError("redis unavailable (test)")
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def aclose(self) -> None:
        return None


class _PerCallSessionFactory:
    """Drop-in for ``async_session_factory`` that builds a FRESH
    engine+session per call.

    The CLI invokes ``async_session_factory()`` inside its own
    ``asyncio.run`` loop. A fixture-scoped engine is bound to the fixture's
    loop and would raise asyncpg "attached to a different loop". Building the
    engine lazily per-call keeps it loop-local; each call disposes its engine
    on context exit.
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def __call__(self) -> _PerCallSessionCtx:
        return _PerCallSessionCtx(self._url)


class _PerCallSessionCtx:
    def __init__(self, url: str) -> None:
        self._url = url
        self._engine: Any = None
        self._session: Any = None

    async def __aenter__(self) -> Any:
        self._engine = create_async_engine(self._url)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)()
        return self._session

    async def __aexit__(self, *exc: Any) -> None:
        await self._session.close()
        await self._engine.dispose()


@pytest_asyncio.fixture
async def wd_session_factory(
    _broker_migrated_url: str,
) -> AsyncIterator[_PerCallSessionFactory]:
    """Per-call session factory bound to the migrated testcontainer, with the
    watchdog-relevant tables truncated for per-test isolation."""
    from sqlalchemy import text

    engine = create_async_engine(_broker_migrated_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE broker_accounts, live_deployments, "
                    "live_portfolio_revisions, live_portfolios RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()
    yield _PerCallSessionFactory(_broker_migrated_url)


@pytest.fixture
def wd_env(monkeypatch, wd_session_factory):  # type: ignore[no-untyped-def]
    """Wire the CLI seams: DB factory → testcontainer, Redis → fake,
    AlertService.send_alert → recording stub. Returns the recorders."""
    store: dict[str, str] = {}
    alert_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(cli, "async_session_factory", wd_session_factory)

    async def _fake_pool() -> _FakeRedis:
        return _FakeRedis(store)

    monkeypatch.setattr(cli, "get_redis_pool", _fake_pool)

    async def _fake_send_alert(self, subject, body, recipients=None, *, level="warning"):  # type: ignore[no-untyped-def]
        alert_calls.append({"subject": subject, "body": body, "level": level})
        return True

    monkeypatch.setattr("msai.services.alerting.AlertService.send_alert", _fake_send_alert)

    # Guard: assert NO HTTP probe to /live/status is ever made (P0: the
    # live-active signal is DB-direct). Any httpx.get call would be a bug.
    http_calls: list[str] = []

    def _fail_http_get(*args, **kwargs):  # type: ignore[no-untyped-def]
        url = args[0] if args else kwargs.get("url", "")
        http_calls.append(url)
        raise AssertionError(f"unexpected HTTP GET during watchdog tick: {url}")

    monkeypatch.setattr("msai.cli.httpx.get", _fail_http_get)

    return {"store": store, "alerts": alert_calls, "http_calls": http_calls}


def _run(args: list[str]):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(app, args)


def _last_token(stdout: str) -> str:
    """Extract the host-consumed action token from its sentinel line.

    Mirrors the host script's parse (``grep -oE 'WATCHDOG_ACTION=[a-z_]+' | tail
    -1 | cut -d= -f2``): the token is sentinel-prefixed so a stray stdout log
    line can't be mistaken for it. Returns "" when no token was emitted (the
    fatal-error path), which the host treats as a failed tick.
    """
    toks = [
        ln.split("=", 1)[1].strip()
        for ln in stdout.splitlines()
        if ln.strip().startswith("WATCHDOG_ACTION=")
    ]
    return toks[-1] if toks else ""


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.usefixtures("wd_env")
def test_idle_down_restarts_and_counter_persists(wd_env) -> None:  # type: ignore[no-untyped-def]
    # ZERO active deployments (truncated table) + injected idle → RESTART.
    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--inject-health",
            "down",
            "--inject-idle",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "decision=RESTART" in result.stdout
    assert "restart_count=1" in result.stdout
    assert _last_token(result.stdout) == "restart"

    # WARNING alert fired for the auto-restart.
    assert wd_env["alerts"], "expected a send_alert call on RESTART"
    assert wd_env["alerts"][-1]["level"] == "warning"
    # No HTTP call to /live/status (DB-direct live-active signal).
    assert wd_env["http_calls"] == []

    # Second tick → counter advances (Redis-persisted), still RESTART.
    result2 = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--inject-health",
            "down",
            "--inject-idle",
        ]
    )
    assert result2.exit_code == 0, result2.stdout
    assert "decision=RESTART" in result2.stdout
    assert "restart_count=2" in result2.stdout
    assert _last_token(result2.stdout) == "restart"


@pytest.mark.usefixtures("wd_env")
def test_live_active_alerts_no_restart(wd_env) -> None:  # type: ignore[no-untyped-def]
    # Seed ONE active (running) LiveDeployment → ALERT_ONLY, no restart token.
    import asyncio

    from tests.integration._deployment_factory import make_live_deployment

    async def _seed() -> None:
        async with cli.async_session_factory() as session:
            await make_live_deployment(session, status="running")
            await session.commit()

    asyncio.run(_seed())

    # NOTE: NOT --inject-idle, so the DB live-active query runs and finds the row.
    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--inject-health",
            "down",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "decision=ALERT_ONLY" in result.stdout
    assert _last_token(result.stdout) == "alert_only"
    # Host must NOT restart under a live deployment.
    assert _last_token(result.stdout) != "restart"
    # The warning alert names the live-trading down condition.
    assert wd_env["alerts"][-1]["level"] == "warning"


@pytest.mark.usefixtures("wd_env")
def test_db_error_is_conservative_alert_only(monkeypatch, wd_env) -> None:  # type: ignore[no-untyped-def]
    # Make the live-active DB query raise → live_deployment_active=None →
    # decide() returns ALERT_ONLY (never restarts when idle can't be confirmed).
    def _boom() -> Any:
        raise RuntimeError("db down (test)")

    monkeypatch.setattr(cli, "async_session_factory", _boom)

    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--inject-health",
            "down",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "decision=ALERT_ONLY" in result.stdout
    assert _last_token(result.stdout) == "alert_only"


@pytest.mark.usefixtures("wd_env")
def test_redis_down_exits_nonzero_with_no_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # UNEXPECTED fatal: the Redis state read raises → non-zero exit, NO valid
    # action-token line (so the Task 3 host guard fires --report-host-failure).
    store: dict[str, str] = {}

    async def _fail_pool() -> _FakeRedis:
        return _FakeRedis(store, fail_on_get=True)

    monkeypatch.setattr(cli, "get_redis_pool", _fail_pool)

    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--inject-health",
            "down",
            "--inject-idle",
        ]
    )
    assert result.exit_code != 0, result.stdout
    # No valid action token emitted.
    valid_tokens = {"none", "restart", "alert_only", "escalate", "recovered"}
    assert _last_token(result.stdout) not in valid_tokens


def test_probe_nonsuccess_status_is_conservative(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A non-2xx /account/health response with a healthy-LOOKING body must NEVER
    # be classified HEALTHY — it's conservatively treated as down/unknown.
    import asyncio

    from msai.services.gateway_watchdog import GatewayHealth

    class _Resp:
        status_code = 500  # non-2xx but NOT auth (401/403) → generic conservative path
        is_success = False

        def json(self) -> dict[str, object]:
            return {"status": "healthy", "gateway_connected": True}

    monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: _Resp())
    connected, health = asyncio.run(
        cli._watchdog_probe_gateway_health(container_running=True, container_health=None)
    )
    assert connected is False
    assert health is GatewayHealth.UNKNOWN


@pytest.mark.usefixtures("wd_env")
def test_report_host_failure_emits_then_throttles(wd_env) -> None:  # type: ignore[no-untyped-def]
    # First host-failure report → CRITICAL emitted + throttle key set.
    r1 = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--report-host-failure",
            "compose --force-recreate ib-gateway failed (rc=1)",
        ]
    )
    assert r1.exit_code == 0, r1.stdout
    assert "host-failure-alert-sent" in r1.stdout
    assert wd_env["alerts"], "expected a CRITICAL host-failure alert on first report"
    assert wd_env["alerts"][-1]["level"] == "critical"
    assert "host-action FAILED" in wd_env["alerts"][-1]["subject"]
    n_after_first = len(wd_env["alerts"])

    # Second report within the throttle window (shared fake-Redis store) → suppressed,
    # NO new alert (anti-storm).
    r2 = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--report-host-failure",
            "compose --force-recreate ib-gateway failed (rc=1) again",
        ]
    )
    assert r2.exit_code == 0, r2.stdout
    assert "host-failure-alert-throttled" in r2.stdout
    assert len(wd_env["alerts"]) == n_after_first, "throttled report must NOT emit a new alert"


def test_report_host_failure_fail_open_when_redis_raises(monkeypatch, wd_env) -> None:  # type: ignore[no-untyped-def]
    # Redis IS the failed dependency that prompted the report → the throttle read
    # raises → FAIL-OPEN: the CRITICAL must STILL be emitted, UNTHROTTLED. (A
    # throttle must never swallow its own failure alert.)
    store: dict[str, str] = {}

    async def _fail_pool() -> _FakeRedis:
        return _FakeRedis(store, fail_on_get=True)

    monkeypatch.setattr(cli, "get_redis_pool", _fail_pool)

    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--report-host-failure",
            "docker daemon unreachable on the host",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "host-failure-alert-sent" in result.stdout  # emitted, NOT throttled
    assert wd_env["alerts"], "fail-open must still emit the CRITICAL"
    assert wd_env["alerts"][-1]["level"] == "critical"


def test_probe_auth_failure_alerts_and_does_not_restart(monkeypatch, wd_env) -> None:  # type: ignore[no-untyped-def]
    # MSAI_API_KEY unset/wrong → /account/health returns 401. The watchdog must NOT
    # treat its OWN auth failure as gateway-down (that would force-recreate a healthy
    # gateway). It alerts (throttled CRITICAL) and decides NONE.
    class _Resp401:
        status_code = 401
        is_success = False

        def json(self) -> dict[str, object]:
            return {"detail": "Not authenticated"}

    # Override wd_env's no-HTTP guard: this path legitimately probes /account/health.
    monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: _Resp401())

    result = _run(
        [
            "system",
            "gateway-watchdog-tick",
            "--container-running",
            "--container-health",
            "healthy",
            "--inject-idle",
        ]
    )
    assert result.exit_code == 0, result.stdout
    assert "decision=NONE" in result.stdout
    assert "probe-auth-failed" in result.stdout
    # Must NOT restart a healthy gateway over a watchdog auth misconfig.
    assert _last_token(result.stdout) == "none"
    # A CRITICAL alert naming the remediation (MSAI_API_KEY) was emitted.
    assert wd_env["alerts"], "expected a CRITICAL auth alert"
    assert wd_env["alerts"][-1]["level"] == "critical"
    assert "MSAI_API_KEY" in wd_env["alerts"][-1]["body"]


@pytest.mark.usefixtures("wd_env")
def test_reset_clears_persisted_state(wd_env) -> None:  # type: ignore[no-untyped-def]
    # Seed a non-empty state key, then `gateway-watchdog-reset` → key gone.
    wd_env["store"][cli._WATCHDOG_STATE_KEY] = '{"down_since": 123.0, "restart_events": [1, 2]}'
    result = _run(["system", "gateway-watchdog-reset"])
    assert result.exit_code == 0, result.stdout
    assert "watchdog state cleared" in result.stdout
    assert cli._WATCHDOG_STATE_KEY not in wd_env["store"]
