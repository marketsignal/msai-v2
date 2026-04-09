"""Entry point: ``python -m msai.live_supervisor``.

Wires the three production services (database session factory, Redis
client, LiveCommandBus) and starts the supervisor loop. SIGTERM is
translated into a clean ``stop_event`` set so the loop drains
gracefully.

This module is intentionally thin — every piece of real logic lives
in :mod:`msai.live_supervisor.main`, :mod:`process_manager`, and
:mod:`heartbeat_monitor` so unit/integration tests can exercise each
piece without standing up the full Docker stack.

Production wiring (Phase 4 task #154 scope-B):

- ``spawn_target = _trading_node_subprocess`` — the real Nautilus
  subprocess entry point that constructs a ``TradingNode``, builds
  IB data + exec clients, and drives the node through its lifecycle
  with the IB disconnect handler + heartbeat thread alive alongside.
- ``payload_factory = _production_payload_factory`` — builds a
  per-deployment :class:`TradingNodePayload` from the
  ``live_deployments`` row (joined with ``strategies``) + the
  process-wide ``settings``. Each spawn gets a fresh, fully-populated
  payload.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any
from uuid import UUID  # noqa: TC003 — used at runtime in _factory signature

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.core.config import settings
from msai.core.logging import setup_logging
from msai.live_supervisor.heartbeat_monitor import HeartbeatMonitor
from msai.live_supervisor.main import run_forever
from msai.live_supervisor.process_manager import ProcessManager
from msai.models import LiveDeployment, Strategy
from msai.services.live_command_bus import LiveCommandBus
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths
from msai.services.nautilus.trading_node_subprocess import (
    TradingNodePayload,
    _trading_node_subprocess,
)

log = logging.getLogger(__name__)


def _build_production_payload_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Closure factory so the returned callable captures the
    session factory without needing a class wrapper. The returned
    callable matches :data:`ProcessManager.PayloadFactory`.

    The factory reads the ``live_deployments`` row (joined with its
    ``strategies`` row) to recover the operator-chosen strategy file,
    class name, and per-deployment config. It then resolves those to
    the two Nautilus importable path strings via
    :func:`resolve_importable_strategy_paths` (which is the same
    helper :class:`BacktestRunner` uses — single source of truth
    for strategy path resolution).

    Settings-sourced fields (``database_url``, ``redis_url``,
    ``ib_account_id``) come from the process-wide
    :data:`msai.core.config.settings` singleton. ``ib_host`` defaults
    to ``127.0.0.1`` and ``ib_port`` to ``4002`` (paper) — the
    operator overrides these via env vars if running against a
    non-default IB Gateway.

    Raises are propagated so :meth:`ProcessManager.spawn` can mark
    the row as ``SPAWN_FAILED_PERMANENT``.
    """

    async def _factory(
        row_id: UUID,
        deployment_id: UUID,
        deployment_slug: str,
        payload_dict: dict[str, Any],  # noqa: ARG001
    ) -> tuple[TradingNodePayload]:
        async with session_factory() as session:
            deployment = (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.id == deployment_id)
                )
            ).scalar_one_or_none()
            if deployment is None:
                raise ValueError(
                    f"deployment {deployment_id} disappeared between "
                    f"phase A and the payload factory"
                )

            strategy = (
                await session.execute(select(Strategy).where(Strategy.id == deployment.strategy_id))
            ).scalar_one_or_none()
            if strategy is None:
                raise ValueError(
                    f"deployment {deployment_id} references strategy "
                    f"{deployment.strategy_id} which does not exist"
                )

            # Resolve ``strategies/example/ema_cross.py`` → Nautilus
            # importable strings. The same helper powers the backtest
            # runner (services/nautilus/backtest_runner.py:341) so
            # live and backtest always agree on how a strategy file
            # turns into an ``ImportableStrategyConfig``.
            paths = resolve_importable_strategy_paths(
                strategy_file=strategy.file_path,
                strategy_class_name=strategy.strategy_class,
            )

            # ``deployment.instruments`` is the stored instrument
            # list (e.g. ``["AAPL.NASDAQ", "MSFT.NASDAQ"]``). Pass
            # through the symbol portion (before the venue suffix)
            # as ``paper_symbols`` — the Nautilus instrument
            # provider expects bare symbols that it then resolves
            # to IB contracts via the security master.
            paper_symbols = [instrument.split(".")[0] for instrument in deployment.instruments]

            # Codex iter3 P1: real-money safety gap.
            #
            # The deployment row carries TWO pieces of ground truth
            # for IB target: ``paper_trading`` (boolean flag set at
            # deployment-creation time) and ``account_id`` (the IB
            # account string the operator requested). The process-
            # wide settings carry ``ib_host`` / ``ib_port`` /
            # ``ib_account_id`` from env vars.
            #
            # Before this check, the payload factory blindly used
            # the settings values, ignoring the deployment row. A
            # supervisor running with ``IB_PORT=4001`` +
            # ``IB_ACCOUNT_ID=U*`` (live) but spawning a deployment
            # with ``paper_trading=True`` would silently connect to
            # the LIVE gateway and submit real-money orders under a
            # row that claims paper. The ``/api/v1/live/start``
            # idempotency layer, the frontend UI, and the operator's
            # own mental model would all be wrong about what
            # account that deployment was using.
            #
            # Fix: use ``deployment.account_id`` (not the settings
            # default) so each row is self-consistent, and validate
            # that ``paper_trading`` matches the ``IB_PORT`` the
            # supervisor is configured to hit. Mismatch raises,
            # which marks the row ``SPAWN_FAILED_PERMANENT`` and
            # ACKs the command — the operator has to fix the
            # supervisor's env or the deployment row before retrying.
            #
            # ``ib_host`` / ``ib_port`` still come from settings
            # because they describe the supervisor's INFRASTRUCTURE
            # connection (which gateway container to reach), not
            # the DEPLOYMENT's business intent (which account to
            # trade under).
            _paper_port = 4002
            _live_port = 4001
            expected_port = _paper_port if deployment.paper_trading else _live_port
            if settings.ib_port != expected_port:
                raise ValueError(
                    f"deployment {deployment_id} has paper_trading="
                    f"{deployment.paper_trading} (expected IB_PORT="
                    f"{expected_port}) but supervisor is configured "
                    f"with IB_PORT={settings.ib_port}. Flipping modes "
                    f"requires restarting the supervisor with matching "
                    f"IB_PORT — otherwise the deployment would connect "
                    f"to the wrong gateway (real money under a paper "
                    f"row, or paper orders under a live row)."
                )

            # ``account_id`` consistency with ``paper_trading``.
            # Paper accounts start with ``DU`` or ``DF`` (FA sub-accounts
            # use ``DFP`` prefix); live accounts don't start with ``D``.
            # This mirrors ``_validate_port_account_consistency`` in
            # ``live_node_config.py`` but catches the mismatch one
            # layer earlier (before build_live_trading_node_config
            # even sees it).
            _paper_prefixes = ("DU", "DF")
            deployment_account = (deployment.account_id or "").strip()
            _is_paper = any(deployment_account.startswith(p) for p in _paper_prefixes)
            if deployment.paper_trading and not _is_paper:
                raise ValueError(
                    f"deployment {deployment_id} has paper_trading=True but "
                    f"account_id='{deployment_account}' does not start with "
                    f"any of {_paper_prefixes}. Paper accounts must use DU*/DF* IDs."
                )
            if not deployment.paper_trading and _is_paper:
                raise ValueError(
                    f"deployment {deployment_id} has paper_trading=False but "
                    f"account_id='{deployment_account}' starts with a paper prefix. "
                    f"Live deployments require non-paper account IDs."
                )

            # Codex iter6 P1: derive ``instrument_id`` + ``bar_type``
            # into the strategy config if the caller didn't set them.
            #
            # Every bundled live strategy
            # (``SmokeMarketOrderConfig``, ``EMACrossConfig``, ...)
            # requires a Nautilus ``InstrumentId`` and ``BarType``
            # string in its config. The ``/api/v1/live/start`` path
            # accepts a bare ``instruments: ["AAPL"]`` list and
            # stores it on ``deployment.instruments``, but the
            # strategy config itself carries the canonical
            # instrument_id + bar_type that the strategy subscribes
            # to. Before this fix, a ``config: {}`` request reached
            # the Nautilus strategy-config parser with no
            # instrument_id and crashed the subprocess during
            # ``node.build()``.
            #
            # The derivation is lossy (we pick the first instrument
            # + assume 1-minute bars), but it's the minimal set of
            # defaults needed so that the smoke test + the frontend
            # "new deployment" flow both work without demanding the
            # caller hand-craft Nautilus-internal identifiers. If
            # the caller explicitly set ``instrument_id`` /
            # ``bar_type`` in the request config, we do NOT
            # override.
            merged_strategy_config = dict(deployment.config or {})
            if deployment.instruments:
                first_instrument = deployment.instruments[0]
                merged_strategy_config.setdefault("instrument_id", first_instrument)
                # Default bar type matches what the backtest path
                # uses for equities intraday bars. Format is
                # documented in Nautilus 1.223.0 model/data.pyx:
                # ``<instrument_id>-<step>-<agg>-<price>-<source>``.
                merged_strategy_config.setdefault(
                    "bar_type",
                    f"{first_instrument}-1-MINUTE-LAST-EXTERNAL",
                )

            nautilus_payload = TradingNodePayload(
                row_id=row_id,
                deployment_id=deployment_id,
                deployment_slug=deployment_slug,
                strategy_path=paths.strategy_path,
                strategy_config_path=paths.config_path,
                strategy_config=merged_strategy_config,
                paper_symbols=paper_symbols,
                canonical_instruments=list(deployment.instruments),
                ib_host=settings.ib_host,
                ib_port=settings.ib_port,
                # Codex iter3 P1: use the deployment row's
                # account_id, not the process-wide settings default.
                # Different deployments on the same supervisor can
                # target different IB accounts (e.g., two paper
                # accounts used for A/B testing) as long as all of
                # them match the supervisor's paper/live port.
                ib_account_id=deployment_account,
                database_url=settings.database_url,
                redis_url=settings.redis_url,
                startup_health_timeout_s=settings.startup_health_timeout_s,
            )
            log.info(
                "trading_node_payload_built",
                extra={
                    "deployment_id": str(deployment_id),
                    "deployment_slug": deployment_slug,
                    "strategy_path": paths.strategy_path,
                    "paper_symbols": paper_symbols,
                    "ib_host": settings.ib_host,
                    "ib_port": settings.ib_port,
                    "account_id": deployment_account,
                    "paper_trading": deployment.paper_trading,
                },
            )
            return (nautilus_payload,)

    return _factory


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Translate SIGTERM + SIGINT into ``stop_event.set()``.

    We use the event loop's signal handler so the flag gets set inside
    the async context; calling ``stop_event.set()`` from a raw signal
    handler would be a thread/async-context mismatch.
    """

    def _shutdown(sig: signal.Signals) -> None:
        logging.getLogger(__name__).info(
            "live_supervisor_shutdown_signal",
            extra={"signal": sig.name},
        )
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)


async def _async_main() -> int:
    setup_logging(settings.environment)
    logger = logging.getLogger(__name__)
    logger.info("live_supervisor_starting")

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True
    )

    bus = LiveCommandBus(redis=redis_client)
    process_manager = ProcessManager(
        db=session_factory,
        redis=redis_client,
        spawn_target=_trading_node_subprocess,
        payload_factory=_build_production_payload_factory(session_factory),
    )
    heartbeat_monitor = HeartbeatMonitor(db=session_factory)

    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)

    try:
        await run_forever(
            bus=bus,
            process_manager=process_manager,
            heartbeat_monitor=heartbeat_monitor,
            stop_event=stop_event,
        )
    finally:
        await redis_client.aclose()
        await engine.dispose()
    logger.info("live_supervisor_stopped")
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    sys.exit(main())
