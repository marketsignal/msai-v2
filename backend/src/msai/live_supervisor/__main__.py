"""Entry point: ``python -m msai.live_supervisor``.

Wires the three production services (database session factory, Redis
client, LiveCommandBus) and starts the supervisor loop. SIGTERM is
translated into a clean ``stop_event`` set so the loop drains
gracefully.

This module is intentionally thin — every piece of real logic lives
in :mod:`msai.live_supervisor.main`, :mod:`process_manager`, and
:mod:`heartbeat_monitor` so unit/integration tests can exercise each
piece without standing up the full Docker stack.

Production wiring (PR 1 multi-account-broker-fleet T11/T12 —
per-account payload factory + per-account TradingNodeConfig):

- ``spawn_target = _trading_node_subprocess`` — the real Nautilus
  subprocess entry point that constructs a ``TradingNode``, builds
  IB data + exec clients, and drives the node through its lifecycle
  with the IB disconnect handler + heartbeat thread alive alongside.
- ``payload_factory = _build_production_payload_factory(...)`` — builds
  a per-deployment :class:`TradingNodePayload` from the
  ``live_deployments`` row (joined with ``strategies``) + the
  process-wide ``settings``. Resolves the Databento native ids +
  venue_dataset_map + canonical→native bar-type map for the per-account
  Databento data + IB-exec topology when ``account_id`` is set
  (PR 1 T11/T12). Each spawn gets a fresh, fully-populated payload.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from msai.models import LiveDeployment, LivePortfolioRevisionStrategy, Strategy
from msai.services.live.deployment_identity import derive_strategy_id_full
from msai.services.live.gateway_router import GatewayRouter
from msai.services.live_command_bus import LiveCommandBus
from msai.services.nautilus.databento_live_config import (
    resolve_databento_targets,
)
from msai.services.nautilus.ibg_client_id import (
    ROLE_EXEC,
    derive_ibg_client_id,
)
from msai.services.nautilus.live_instrument_bootstrap import (
    exchange_local_today,
)
from msai.services.nautilus.security_master.live_resolver import (
    LiveResolverError,
    lookup_for_live,
)
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths
from msai.services.nautilus.trading_node_subprocess import (
    StrategyMemberPayload,
    TradingNodePayload,
    _trading_node_subprocess,
)
from msai.services.strategy_registry import compute_file_hash

# Canonical strategy-side venue (per ``nautilus.md`` architectural rule #3
# — "Pin venue names per environment"). The per-account broker fleet
# (PR 1 T11/T12) routes Databento bars onto this canonical venue via the
# :class:`SymbologyShimActor`. Strategies subscribe with this suffix; the
# shim translates inbound native bars to it.
_IBKR_VENUE_SUFFIX: str = "IBKR"

log = logging.getLogger(__name__)


def _strip_venue_suffix(instrument: str) -> str:
    """Return the symbol root from an instrument id, stripping the
    venue suffix once.

    F8 fix (Codex iter 2 P2 / pr-toolkit code-reviewer convergent
    finding): share-class symbols like ``BRK.B`` are supported by the
    security_master, and their fully-qualified form is ``BRK.B.NYSE``.
    The old ``inst.split(".")[0]`` splits at the FIRST ``.`` and returns
    ``BRK`` — wrong root. Using ``rsplit(".", 1)`` strips one trailing
    venue token and returns ``BRK.B``.

    Inputs without a ``.`` (already-bare symbols like ``AAPL``) pass
    through unchanged.
    """
    if "." not in instrument:
        return instrument
    return instrument.rsplit(".", 1)[0]


def _build_production_payload_factory(
    session_factory: async_sessionmaker[AsyncSession],
    gateway_router: GatewayRouter | None = None,
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
    to ``127.0.0.1`` and ``ib_port`` to ``4004`` (gnzsnz socat proxy
    for paper). IB Gateway itself binds to 127.0.0.1:4002 internally —
    cross-container clients MUST use the socat port. The operator
    overrides these via env vars when running against a non-default
    IB Gateway (e.g. ``IB_PORT=4003`` for live mode).

    When a :class:`GatewayRouter` is provided and the deployment has
    an ``ib_login_key``, the factory resolves the IB host/port from
    the router instead of using the process-wide settings. This
    enables multi-login topologies where each IB login connects to a
    dedicated gateway container.

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

            # Compute the exchange-local date ONCE per spawn and thread
            # it through every futures-rollover-sensitive call site.
            # Without this, the supervisor's canonicalization and the
            # subprocess's provider config can resolve to different
            # front-months if the spawn crosses midnight on a quarterly
            # roll day — which would cause the strategy to subscribe to
            # a bar stream that doesn't exist in the loaded cache.
            spawn_today = exchange_local_today()

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
            deployment_account = (deployment.account_id or "").strip()

            # ---------------------------------------------------------
            # Resolve IB host/port: GatewayRouter (when configured) or
            # fall back to process-wide settings.
            # ---------------------------------------------------------
            # F5 fix (silent-failure-hunter F2): the previous fallback
            # collapsed three independent conditions into one ``else``:
            # (1) ``ib_login_key`` empty/None, (2) ``gateway_router`` None,
            # (3) router exists but ``is_multi_login`` is False. The
            # missing branch — ``ib_login_key`` is non-empty but doesn't
            # match any router entry — silently fell through to
            # ``settings.ib_host/port``, meaning a deployment for account
            # A on login key X could end up routed to whatever the
            # process-wide IB env vars point at (login Y). Real-money
            # mis-routing.
            #
            # New rule: when ``ib_login_key`` is set AND a router exists,
            # ALWAYS call ``gateway_router.resolve(...)`` (regardless of
            # ``is_multi_login``). Let ``ValueError`` propagate up to the
            # process_manager's permanent-catch — translates to
            # ``SPAWN_FAILED_PERMANENT`` via the existing error dispatch.
            # Single-login routers still validate (good — they reject
            # typos at the boundary instead of silently picking env vars).
            # The fall-through to ``settings.ib_host/port`` is allowed
            # ONLY when ``ib_login_key`` is None/empty (legacy row) OR
            # equals the migration sentinel ``"default"`` (Codex iter 12
            # P2: migration ``t8o9p0q1r2s3`` backfills NULL ib_login_key
            # to ``"default"`` so the column can be NOT NULL — these
            # rows pre-date routing and must continue to warm-restart
            # via ``settings.ib_host/port``).
            legacy_default_login_key = "default"
            is_routed = (
                deployment.ib_login_key is not None
                and deployment.ib_login_key != legacy_default_login_key
                and deployment.ib_login_key != ""
            )
            if is_routed and gateway_router is not None:
                endpoint = gateway_router.resolve(deployment.ib_login_key)
                ib_host = endpoint.host
                ib_port = endpoint.port
                # Codex iter 15 P2: when GATEWAY_CONFIG declares an
                # ``accounts=`` binding for this login (e.g.
                # ``marin1016test:host:port:accounts=DUP733214|DUP733215``),
                # enforce that ``deployment.account_id`` is one of the
                # bound accounts. Without this check, a deployment could
                # request a login that routes successfully but submit
                # orders against an unbound account_id — defeating the
                # declarative account-to-login isolation.
                #
                # Bindings are opt-in: legacy ``login:host:port`` entries
                # (no ``accounts=`` segment) get an empty list and skip
                # the check.
                bound_accounts = gateway_router.accounts_for(deployment.ib_login_key)
                if bound_accounts and deployment.account_id not in bound_accounts:
                    raise ValueError(
                        f"account_id={deployment.account_id!r} is not bound to "
                        f"ib_login_key={deployment.ib_login_key!r} in GATEWAY_CONFIG "
                        f"(bound accounts: {bound_accounts!r}). Refusing to route — "
                        "the declarative account-to-login binding would be bypassed."
                    )
            elif is_routed:
                # Codex iter 7 P2-1: deployment requests a specific
                # ``ib_login_key`` but the supervisor was started without
                # ``GATEWAY_CONFIG`` (router is None). Falling through to
                # ``settings.ib_host/port`` would silently route the deployment
                # to the wrong broker. Fail-closed instead.
                raise ValueError(
                    f"deployment requests ib_login_key={deployment.ib_login_key!r} "
                    f"but no GATEWAY_CONFIG is set on the supervisor — "
                    f"refuse to route to settings.ib_host/port (would risk "
                    f"submitting orders against the wrong broker container)"
                )
            else:
                ib_host = settings.ib_host
                ib_port = settings.ib_port

            # Gotcha #6 guard: validate the ROUTED port (ib_port, not
            # settings.ib_port) against (a) deployment.paper_trading and
            # (b) deployment.account_id. In multi-login setups the
            # supervisor default may be paper (4002/4004) while a given
            # login routes to live (4001/4003) — we must validate the
            # endpoint the subprocess will actually hit.
            from msai.services.nautilus.ib_port_validator import (
                validate_port_account_consistency,
                validate_port_vs_paper_trading,
            )

            try:
                validate_port_vs_paper_trading(
                    ib_port,
                    paper_trading=deployment.paper_trading,
                )
                validate_port_account_consistency(ib_port, deployment_account)
            except ValueError as exc:
                raise ValueError(f"deployment {deployment_id}: {exc}") from exc

            # ---------------------------------------------------------
            # Branch: portfolio-based vs single-strategy deployment
            # ---------------------------------------------------------
            if deployment.portfolio_revision_id is not None:
                # Portfolio-based deployment — load all revision members
                members: list[LivePortfolioRevisionStrategy] = list(
                    (
                        await session.execute(
                            select(LivePortfolioRevisionStrategy)
                            .where(
                                LivePortfolioRevisionStrategy.revision_id
                                == deployment.portfolio_revision_id
                            )
                            .order_by(LivePortfolioRevisionStrategy.order_index)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not members:
                    raise ValueError(
                        f"deployment {deployment_id} references portfolio "
                        f"revision {deployment.portfolio_revision_id} which "
                        f"has no strategy members"
                    )

                # Load all Strategy rows referenced by members. SUPERVISOR
                # path opts into the soft-delete filter (plan R20): a live
                # deployment whose strategy has since been archived must
                # still resolve its Strategy row so the supervisor can
                # construct the TradingNode payload without crashing.
                member_strategy_ids = [m.strategy_id for m in members]
                strategies_by_id: dict[UUID, Strategy] = {}
                for strat_row in (
                    (
                        await session.execute(
                            select(Strategy)
                            .where(Strategy.id.in_(member_strategy_ids))
                            .execution_options(include_deleted=True)
                        )
                    )
                    .scalars()
                    .all()
                ):
                    strategies_by_id[strat_row.id] = strat_row

                missing = [sid for sid in member_strategy_ids if sid not in strategies_by_id]
                if missing:
                    raise ValueError(
                        f"deployment {deployment_id}: strategies not found: "
                        f"{[str(s) for s in missing]}"
                    )

                # Build a StrategyMemberPayload per member
                strategy_members: list[StrategyMemberPayload] = []
                all_paper_symbols: list[str] = []
                all_canonical_instruments: list[str] = []

                for member in members:
                    strat = strategies_by_id[member.strategy_id]
                    paths = resolve_importable_strategy_paths(
                        strategy_file=strat.file_path,
                        strategy_class_name=strat.strategy_class,
                    )

                    # Compute per-member code hash
                    member_code_hash = ""
                    try:
                        from pathlib import Path as _Path

                        rel = _Path(strat.file_path)
                        if rel.is_absolute():
                            abs_path = rel
                        elif rel.parts and rel.parts[0] == "strategies":
                            abs_path = settings.strategies_root.joinpath(*rel.parts[1:])
                        else:
                            abs_path = settings.strategies_root / rel
                        if abs_path.is_file():
                            member_code_hash = compute_file_hash(abs_path)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "strategy_code_hash_failed",
                            extra={"strategy_id": str(strat.id)},
                        )

                    strategy_id_full = derive_strategy_id_full(
                        strat.strategy_class,
                        deployment_slug,
                        member.order_index,
                    )

                    # Per-member instruments: pre-paper_symbols + registry-backed
                    # resolution. ``lookup_for_live`` raises
                    # ``RegistryMissError`` / ``RegistryIncompleteError`` /
                    # ``UnsupportedAssetClassError`` / ``AmbiguousRegistryError``
                    # (all subclass ``LiveResolverError`` → ``ValueError``), so
                    # ``ProcessManager``'s permanent-catch fires and dispatches
                    # on subtype to the specific ``FailureKind``.
                    member_paper_symbols = [
                        _strip_venue_suffix(inst) for inst in member.instruments
                    ]

                    # Defensive guard — empty member.instruments is a programmer
                    # bug (portfolio revision freeze should have rejected it).
                    # ``strategy_id_full`` is the LOCAL variable from above, NOT
                    # an attribute on the ORM row.
                    if not member.instruments:
                        raise ValueError(
                            f"strategy member {strategy_id_full!r} has no "
                            "instruments — portfolio freeze should have "
                            "rejected this revision"
                        )

                    resolved_instruments = await lookup_for_live(
                        list(member.instruments),
                        as_of_date=spawn_today,
                        session=session,
                    )
                    member_canonical = [r.canonical_id for r in resolved_instruments]
                    member_resolved = tuple(resolved_instruments)

                    # Derive instrument_id + bar_type into the member's
                    # config, same logic as the single-strategy path.
                    member_config = dict(member.config or {})
                    if member_canonical:
                        first_inst = member_canonical[0]
                        first_user_inst = member.instruments[0]
                        _is_fx = any(
                            x in first_inst.upper()
                            for x in ("IDEALPRO", "CASH", "EUR", "GBP", "JPY", "AUD", "CHF")
                        )
                        _price_type = "MID" if _is_fx else "LAST"
                        user_root = _strip_venue_suffix(first_user_inst)
                        canonical_root = _strip_venue_suffix(first_inst)
                        if user_root != canonical_root:
                            member_config["instrument_id"] = first_inst
                            existing_bt = member_config.get("bar_type", "")
                            if not existing_bt:
                                member_config["bar_type"] = (
                                    f"{first_inst}-1-MINUTE-{_price_type}-EXTERNAL"
                                )
                            elif existing_bt.startswith(first_user_inst + "-"):
                                member_config["bar_type"] = (
                                    first_inst + existing_bt[len(first_user_inst) :]
                                )
                        else:
                            member_config.setdefault("instrument_id", first_inst)
                            member_config.setdefault(
                                "bar_type",
                                f"{first_inst}-1-MINUTE-{_price_type}-EXTERNAL",
                            )

                    strategy_members.append(
                        StrategyMemberPayload(
                            strategy_id=strat.id,
                            strategy_path=paths.strategy_path,
                            strategy_config_path=paths.config_path,
                            strategy_config=member_config,
                            strategy_code_hash=member_code_hash,
                            strategy_id_full=strategy_id_full,
                            instruments=member_paper_symbols,
                            resolved_instruments=member_resolved,
                        )
                    )
                    all_paper_symbols.extend(member_paper_symbols)
                    all_canonical_instruments.extend(member_canonical)

                # De-duplicate across all members
                paper_symbols = sorted(set(all_paper_symbols))
                canonical_instrument_ids = sorted(set(all_canonical_instruments))

                # ----------------------------------------------------
                # PR 1 T12: per-account Databento + IB-exec resolution.
                # ----------------------------------------------------
                # When the deployment carries an ``account_id`` (always
                # true post-Task 11), pre-resolve the three fields the
                # subprocess needs to construct the per-account
                # TradingNodeConfig via
                # :func:`build_per_account_trading_node_config`:
                #
                #   1. ``native_instrument_ids`` + ``venue_dataset_map``
                #      — resolved via T10's :func:`resolve_databento_targets`
                #      which calls the symbology shim per canonical id.
                #   2. ``canonical_to_native_bar_types`` — built here
                #      by zipping each member's canonical bar_type
                #      (canonical ``.IBKR`` venue suffix) with its
                #      native equivalent (replace ``.IBKR`` with the
                #      shim's native venue from the venue_dataset_map).
                #   3. ``ibg_client_id`` — derived from the deployment
                #      slug via the shared helper
                #      :func:`derive_ibg_client_id` (single source of
                #      truth, Codex iter 1 P1-5).
                #
                # PR 1 scope is EQUITIES ONLY. Any non-equity asset
                # class trips :class:`NotImplementedError` from the
                # shim — we catch it and skip the per-account fields
                # so the legacy builder still spawns the deployment
                # (futures + options bridge work is deferred to PR 2).
                native_instrument_ids: list[str] = []
                venue_dataset_map: dict[str, str] = {}
                canonical_to_native_bar_types: dict[str, str] = {}
                ibg_client_id = 0
                if deployment_account:
                    # Build the .IBKR-canonical id list from each
                    # member's first canonical instrument. The shim
                    # is keyed per-symbol so passing each canonical
                    # root once is sufficient; ``resolve_databento_targets``
                    # de-dups internally if needed.
                    canonical_ibkr_ids: list[str] = []
                    seen_canonical_roots: set[str] = set()
                    for canonical_str in canonical_instrument_ids:
                        if "." not in canonical_str:
                            continue
                        root = _strip_venue_suffix(canonical_str)
                        if not root or root in seen_canonical_roots:
                            continue
                        seen_canonical_roots.add(root)
                        canonical_ibkr_ids.append(f"{root}.{_IBKR_VENUE_SUFFIX}")

                    try:
                        resolved_targets = await resolve_databento_targets(
                            canonical_ids=canonical_ibkr_ids,
                            session=session,
                            as_of_date=spawn_today,
                        )
                        native_instrument_ids = list(resolved_targets.native_instrument_ids)
                        venue_dataset_map = dict(resolved_targets.venue_dataset_map)

                        # Build canonical_to_native_bar_types by zipping
                        # each member's ``bar_type`` (canonical, e.g.
                        # ``AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL``)
                        # with the native equivalent. The shim's
                        # outbound subscription requires the .IBKR
                        # canonical suffix per architectural rule #3,
                        # so we substitute the venue portion of the
                        # bar_type prefix:
                        #
                        #   AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL
                        #     -> AAPL.IBKR-1-MINUTE-LAST-EXTERNAL   (canonical key)
                        #     -> AAPL.XNAS-1-MINUTE-LAST-EXTERNAL   (native value)
                        #
                        # ``native_instrument_ids`` is order-preserved
                        # against ``canonical_ibkr_ids`` (T10 builds the
                        # list in that order), so we can zip them.
                        root_to_native_iid = {
                            _strip_venue_suffix(canonical_ibkr_id): native_iid
                            for canonical_ibkr_id, native_iid in zip(
                                canonical_ibkr_ids, native_instrument_ids, strict=True
                            )
                        }
                        for member_payload in strategy_members:
                            bar_type_str = str(member_payload.strategy_config.get("bar_type") or "")
                            if not bar_type_str:
                                continue
                            # bar_type format: "SYM.VENUE-..."
                            prefix, dash, spec_tail = bar_type_str.partition("-")
                            if not dash or "." not in prefix:
                                continue
                            sym_root = _strip_venue_suffix(prefix)
                            native_iid = root_to_native_iid.get(sym_root)
                            if not native_iid:
                                continue
                            canonical_key = f"{sym_root}.{_IBKR_VENUE_SUFFIX}-{spec_tail}"
                            native_value = f"{native_iid}-{spec_tail}"
                            canonical_to_native_bar_types[canonical_key] = native_value

                        ibg_client_id = derive_ibg_client_id(deployment_slug, ROLE_EXEC)
                    except (NotImplementedError, LiveResolverError) as exc:
                        # PR 1 equities-only: futures/options/fx fall
                        # back to the legacy builder. Two trigger paths:
                        #
                        # 1. ``NotImplementedError`` — the shim's
                        #    asset-class guard fires for a member whose
                        #    raw_symbol matches a futures/option/fx
                        #    registry row (the intended fallback path).
                        #
                        # 2. ``LiveResolverError`` (Codex iter 17 P1) —
                        #    e.g. ``RegistryMissError`` when the
                        #    supervisor strips the venue suffix off
                        #    ``ESM6.CME`` and the shim's bare-symbol
                        #    lookup of ``ESM6`` doesn't match any
                        #    raw_symbol (the registry stores futures
                        #    as raw_symbol=``ES`` + alias=``ESM6.CME``;
                        #    bare ``ESM6`` skips the alias-fallback
                        #    branch in ``lookup_for_live``). Catching
                        #    the broader ``LiveResolverError`` base
                        #    class lets ANY non-equity registry miss
                        #    fall through to the legacy IB-data builder
                        #    where contract resolution does work via
                        #    ``find_by_alias`` upstream. A genuine
                        #    equity miss would also fail under the
                        #    legacy path — equivalent end result,
                        #    correct outcome for non-equities.
                        log.info(
                            "per_account_topology_skipped_non_equity",
                            extra={
                                "deployment_id": str(deployment_id),
                                "reason": str(exc),
                                "exc_type": type(exc).__name__,
                            },
                        )
                        native_instrument_ids = []
                        venue_dataset_map = {}
                        canonical_to_native_bar_types = {}
                        ibg_client_id = 0

                # Use first member's paths for backward-compat fields
                first_member = strategy_members[0]
                nautilus_payload = TradingNodePayload(
                    row_id=row_id,
                    deployment_id=deployment_id,
                    deployment_slug=deployment_slug,
                    strategy_id=deployment.strategy_id,
                    # ``strategy_code_hash`` column was dropped in Task 11
                    # — derive from the first member's per-strategy hash.
                    strategy_code_hash=strategy_members[0].strategy_code_hash
                    if strategy_members
                    else "",
                    strategy_path=first_member.strategy_path,
                    strategy_config_path=first_member.strategy_config_path,
                    strategy_config=first_member.strategy_config,
                    paper_symbols=paper_symbols,
                    canonical_instruments=canonical_instrument_ids,
                    spawn_today_iso=spawn_today.isoformat(),
                    ib_host=ib_host,
                    ib_port=ib_port,
                    ib_account_id=deployment_account,
                    database_url=settings.database_url,
                    redis_url=settings.redis_url,
                    startup_health_timeout_s=settings.startup_health_timeout_s,
                    strategy_members=strategy_members,
                    # PR 1 T12 per-account fields. Empty when the
                    # equities-only shim couldn't resolve (futures/
                    # options/fx) — subprocess falls back to legacy
                    # builder via ``use_per_account_topology = False``.
                    ibg_client_id=ibg_client_id,
                    native_instrument_ids=native_instrument_ids,
                    venue_dataset_map=venue_dataset_map,
                    canonical_to_native_bar_types=canonical_to_native_bar_types,
                )
                log.info(
                    "trading_node_payload_built",
                    extra={
                        "deployment_id": str(deployment_id),
                        "deployment_slug": deployment_slug,
                        "portfolio_revision_id": str(deployment.portfolio_revision_id),
                        "member_count": len(strategy_members),
                        "paper_symbols": paper_symbols,
                        "ib_host": ib_host,
                        "ib_port": ib_port,
                        "account_id": deployment_account,
                        "paper_trading": deployment.paper_trading,
                        "ib_login_key": deployment.ib_login_key,
                        "ibg_client_id": ibg_client_id,
                        "native_instrument_ids": native_instrument_ids,
                        "venue_dataset_map": venue_dataset_map,
                        "canonical_to_native_bar_types_count": len(canonical_to_native_bar_types),
                    },
                )

            else:
                # Dead path post-Task 11: portfolio_revision_id is NOT NULL,
                # so the ``if`` branch above always runs. Guard defensively.
                raise ValueError(
                    f"deployment {deployment_id} has no portfolio_revision_id "
                    f"— this should be impossible after the Task 10 backfill"
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
    # ``setup_logging`` configures structlog. The supervisor modules
    # (__main__.py, main.py, process_manager.py, heartbeat_monitor.py)
    # and ``live_command_bus`` all use stdlib ``logging.getLogger`` —
    # without an explicit basicConfig those INFO records are dropped
    # by stdlib's lastResort (WARNING+ only), which is exactly why
    # the 2026-04-15 drill saw zero log output from the running
    # supervisor. Configure stdlib alongside structlog so every
    # module's logs reach stderr (and therefore ``docker logs``).
    logging.basicConfig(
        level=logging.DEBUG if settings.environment.lower() == "development" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    setup_logging(settings.environment)
    logger = logging.getLogger(__name__)
    logger.info("live_supervisor_starting")

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True
    )

    gateway_config = os.environ.get("GATEWAY_CONFIG")
    gateway_router = GatewayRouter(gateway_config) if gateway_config else None
    if gateway_router and gateway_router.is_multi_login:
        logger.info(
            "gateway_router_initialized",
            extra={
                "login_keys": gateway_router.login_keys,
                "config": gateway_config,
            },
        )

    # Codex iter 13 P2: docker-compose interpolation runs BEFORE profile
    # selection, so ``:?`` guards on broker-only services would break
    # default-profile ``docker compose config / up``. Surface missing
    # broker-profile envs at supervisor startup instead — the supervisor
    # only runs under the broker profile, so this is the right boundary.
    if not gateway_config:
        logger.warning(
            "supervisor_started_without_gateway_config "
            "— per-account deployments will be rejected by the "
            "payload factory's fail-closed branch. Set GATEWAY_CONFIG "
            "in .env if you intend to deploy with ib_login_key set."
        )
    if not settings.databento_api_key:
        logger.warning(
            "supervisor_started_without_databento_api_key "
            "— per-account TradingNode subprocesses will fail when "
            "constructing the Databento live data client. Set "
            "DATABENTO_API_KEY in .env to enable per-account deployments."
        )

    bus = LiveCommandBus(redis=redis_client)
    process_manager = ProcessManager(
        db=session_factory,
        redis=redis_client,
        spawn_target=_trading_node_subprocess,
        payload_factory=_build_production_payload_factory(
            session_factory, gateway_router=gateway_router
        ),
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
