"""Live ``TradingNodeConfig`` builder.

Constructs the Nautilus ``TradingNodeConfig`` that the live trading
subprocess hands to ``TradingNode``. Uses Nautilus natives for every
engine and client config so we get reconciliation, risk checks, and IB
integration "for free" (decision: don't reinvent what Nautilus already
provides — see the natives audit).

Current behavior (F7 comment-drift fix — the original "Phase 1 / Phase 3 /
Phase 4 follow-ups" notes referenced work that has since landed inline):

- ``cache.database`` and ``message_bus.database`` are wired to Redis via
  :func:`build_redis_database_config`. Both writers and the
  :class:`PositionReader` cold path share the same construction so
  username/password/TLS survive (Codex batch 8 P1).
- ``load_state`` and ``save_state`` are ``True`` — Nautilus's built-in
  state persistence rehydrates strategy state across restarts.
- ``message_bus`` runs ``stream_per_topic=False`` with the
  trader-prefixed stream so wildcard consumption from FastAPI works.
- ``buffer_interval_ms=None`` (write-through) on both cache and
  message_bus per gotcha #7.

Two Nautilus gotchas drive the IB client wiring:

- **Gotcha #3** — two ``TradingNode`` clients on the same IB Gateway
  with the same ``ibg_client_id`` silently disconnect each other. Each
  deployment gets a unique data-client id AND a unique exec-client id,
  derived deterministically from its ``deployment_id`` UUID so a restart
  reuses the SAME ids (otherwise IB Gateway sees a "new" client and
  the old one's open orders / subscriptions get stranded).
- **Gotcha #6** — port 4002 (paper) with a live account_id (or 4001 +
  paper account) is a silent data-flow killer: IB Gateway accepts the
  connection but provides no data. Validated at config-build time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from datetime import date

    from msai.services.nautilus.security_master.live_resolver import (
        ResolvedInstrument,
    )
    from msai.services.nautilus.trading_node_subprocess import StrategyMemberPayload

from nautilus_trader.adapters.interactive_brokers.common import IB_VENUE
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
)
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.config import DatabaseConfig, MessageBusConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.live.config import (
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    TradingNodeConfig,
)
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import TraderId
from pydantic import BaseModel, Field

from msai.core.config import settings
from msai.services.nautilus.databento_live_config import (
    build_databento_data_client_config,
)
from msai.services.nautilus.ib_port_validator import (
    validate_port_account_consistency,
)
from msai.services.nautilus.ibg_client_id import (
    ROLE_DATA,
    ROLE_EXEC,
    derive_ibg_client_id,
)
from msai.services.nautilus.live_instrument_bootstrap import (
    build_ib_instrument_provider_config,
    build_ib_instrument_provider_config_from_resolved,
)

# Re-export the role salt constants so legacy callers that imported them
# from this module (or grepped for them here when wiring the live status
# surfacing in Task T14) still find them at the historical location.
__all__ = [
    "ROLE_DATA",
    "ROLE_EXEC",
    "build_live_trading_node_config",
    "build_per_account_strategy_configs",
    "build_per_account_trading_node_config",
    "build_portfolio_trading_node_config",
    "derive_ibg_client_id",
]

# US equity venues — Nautilus VENUE component of InstrumentId for stocks
# traded on US exchanges. When `manage_stop=True` triggers Nautilus's
# built-in `market_exit()` on stop, the resulting market orders inherit
# the IB account's default TIF. IB's stock accounts default to DAY, but
# Nautilus emits GTC unless overridden — IB then rejects with error
# 10349 "TIF not allowed for the order" + cancels in the same message
# IB also CONFIRMS the order accepted, producing a phantom fill in
# Nautilus's view (Bug #2 in live-deploy-safety-trio). Setting
# `market_exit_time_in_force="DAY"` for US-equity venues matches the
# account preset and avoids the cancel/fill race.
_US_EQUITY_VENUES: frozenset[str] = frozenset(
    {"NASDAQ", "NYSE", "ARCA", "BATS", "IEX", "AMEX", "XNAS", "XNYS"}
)


def _has_us_equity_venue(instrument_ids: list[str]) -> bool:
    """Return True if any of the given canonical instrument IDs targets
    a US-equity venue. Each ID is expected to be ``"SYMBOL.VENUE"``;
    inputs that don't match the shape are treated as non-US-equity
    (caller should keep the GTC default).

    Codex iter 3 F3: use ``rpartition('.')`` so share-class symbols like
    ``BRK.B.NYSE`` correctly resolve their venue to ``NYSE``, not ``B.NYSE``.
    A plain ``partition('.')`` would split at the first dot and return
    ``("BRK", ".", "B.NYSE")`` — the venue check would then fail to match
    any US-equity venue and the TIF=DAY override would be silently dropped.
    """
    for instrument_id in instrument_ids:
        _, _, venue = instrument_id.rpartition(".")
        if venue and venue.upper() in _US_EQUITY_VENUES:
            return True
    return False


def _strategy_us_equity_tif_overrides(
    strategy_config: dict[str, Any],
    extra_instruments: list[str] | None = None,
) -> dict[str, Any]:
    """Return the dict fragment to merge into a strategy's config when
    its instruments target a US-equity venue. Empty dict otherwise.

    Reads three sources of instrument IDs:
    - ``strategy_config["instruments"]`` (canonical IDs as a list)
    - ``strategy_config["instrument_id"]`` (single-instrument convention)
    - ``extra_instruments`` — the authoritative per-member list from
      ``StrategyMemberPayload.instruments`` for the portfolio path
      (PR #65 Codex P2). The portfolio builder only writes
      ``instrument_id`` into the per-member config but the
      multi-instrument truth lives on the payload; a strategy whose
      first config instrument is non-US-equity but whose payload
      contains a US-equity member would otherwise miss the TIF=DAY
      override and re-trigger the IB error-10349 cancel-fill race.
    """
    ids: list[str] = []
    if isinstance(strategy_config.get("instruments"), list):
        ids.extend(str(x) for x in strategy_config["instruments"])
    if "instrument_id" in strategy_config and strategy_config["instrument_id"]:
        ids.append(str(strategy_config["instrument_id"]))
    if extra_instruments:
        ids.extend(str(x) for x in extra_instruments)
    if _has_us_equity_venue(ids):
        # Emit the integer value of the Nautilus TimeInForce enum (DAY=5
        # in 1.225+). msgspec deserializes StrategyConfig from JSON and
        # rejects string variants — see `nautilus_trader/model/enums.pyx`.
        return {"market_exit_time_in_force": int(TimeInForce.DAY)}
    return {}


# Canonical strategy-side venue for the per-account broker fleet (PR 1).
# Per ``nautilus.md`` architectural rule #3 ("Pin venue names per
# environment — IBKR for live") and the council's symbology decision,
# strategies subscribe under ``.IBKR`` and the ``SymbologyShimActor``
# republishes inbound native Databento bars onto that canonical venue.
# Centralized here so the supervisor's payload factory and this
# module's strategy-config rewriter agree on the same suffix.
_IBKR_VENUE_SUFFIX: str = "IBKR"


def _rewrite_bar_type_to_ibkr(bar_type_str: str) -> str:
    """Rewrite a ``bar_type`` string so its venue component becomes ``IBKR``.

    Used ONLY for the strategy's ``bar_type`` field on the per-account
    topology — the inbound data path. The strategy subscribes to
    ``<SYM>.IBKR-1-MINUTE-LAST-EXTERNAL`` (matching what the
    ``SymbologyShimActor`` republishes from native Databento bars onto the
    canonical bus topic) regardless of the LISTING venue the operator
    typed in. The ``instrument_id`` field is intentionally NOT rewritten
    by this helper — order routing stays on the LISTING venue where the
    IB exec adapter's contract qualification (preloaded via
    :func:`build_ib_instrument_provider_config_from_resolved`) knows the
    right ``primaryExchange``.

    Codex iter 3 F1 SPLIT: the previous ``_rewrite_venue_to_ibkr`` helper
    rewrote BOTH ``instrument_id`` and ``bar_type``. That caused strategy
    orders to be minted on synthetic ``<SYM>.IBKR`` ids that the IB exec
    provider cache (keyed by LISTING venue contracts) couldn't resolve;
    the adapter would treat ``IBKR`` as a primary-exchange/MIC at order
    submit time, fail qualification, and the order would never reach IB.

    Codex iter 3 F3: use ``rsplit('.', 1)`` so share-class symbols like
    ``BRK.B.NYSE`` correctly resolve to root ``BRK.B`` (not ``BRK``).
    The supervisor's Databento bar-type map (built with ``rsplit`` in
    ``live_supervisor/__main__.py:_strip_venue_suffix``) is keyed on
    ``BRK.B.IBKR-...``; this helper MUST produce the same shape so the
    strategy's bar-type subscription matches the shim's republish topic.

    Args:
        bar_type_str: A canonical Nautilus bar-type string like
            ``"AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"`` or
            ``"BRK.B.NYSE-1-MINUTE-LAST-EXTERNAL"``. Inputs without a
            ``.`` in the symbol prefix (already-bare, or already in
            ``.IBKR`` form) pass through unchanged.

    Returns:
        The same string with the venue token replaced by ``IBKR``.
    """
    prefix, dash, spec_tail = bar_type_str.partition("-")
    if "." not in prefix:
        return bar_type_str
    # F3: rsplit so dotted share-class roots (``BRK.B``) survive intact.
    sym_root, _, _venue = prefix.rpartition(".")
    if not sym_root:
        return bar_type_str
    rewritten_prefix = f"{sym_root}.{_IBKR_VENUE_SUFFIX}"
    if dash:
        return f"{rewritten_prefix}-{spec_tail}"
    return rewritten_prefix


def build_per_account_strategy_configs(
    strategy_members: list[StrategyMemberPayload],
    *,
    deployment_slug: str,
) -> list[ImportableStrategyConfig]:
    """Build ``ImportableStrategyConfig``s for the per-account topology.

    Codex iter 1 P1-1 + P1-3 of PR 1: the per-account router used to
    bypass strategy wiring entirely — the spawned ``TradingNode``
    contained the ``SymbologyShimActor`` but zero trading strategies,
    so a deployment could start without ever subscribing or trading
    (P1-1). And when strategies were added back, their config
    ``bar_type`` still referenced the LISTING venue (``AAPL.NASDAQ``)
    while the shim republishes onto ``AAPL.IBKR``, so no live bars
    ever reached the strategy (P1-3).

    **Codex iter 3 F1 (architectural decision):** the symbology shim
    canonicalizes the DATA path (bar topics) to ``.IBKR``, NOT the EXEC
    path. This helper rewrites ONLY the ``bar_type`` to ``.IBKR``
    (matching what :class:`SymbologyShimActor` republishes); the
    ``instrument_id`` field is LEFT on its LISTING venue
    (``.NASDAQ``/``.NYSE``/``.ARCA``) so order submission resolves
    cleanly via the IB exec adapter's preloaded contract cache.

    Mirrors the legacy ``build_portfolio_trading_node_config`` strategy
    loop for the non-venue fields (``order_id_tag`` parsing,
    ``manage_stop=True``, US-equity TIF override) so warm-restart
    state-reload and the audit trail behave identically across the
    two topologies.
    """
    strategy_configs: list[ImportableStrategyConfig] = []
    for member in strategy_members:
        _parts = member.strategy_id_full.split("-", 1)
        order_id_tag = _parts[1] if len(_parts) >= 2 else deployment_slug

        # Rewrite ONLY the bar_type venue on the strategy_config copy.
        # ``instrument_id`` stays on the LISTING venue — see F1 docstring
        # on ``_rewrite_bar_type_to_ibkr`` for why exec lookups need the
        # listing-venue suffix.
        rewritten_config: dict[str, Any] = dict(member.strategy_config)
        if "bar_type" in rewritten_config and isinstance(rewritten_config["bar_type"], str):
            rewritten_config["bar_type"] = _rewrite_bar_type_to_ibkr(rewritten_config["bar_type"])

        strategy_configs.append(
            ImportableStrategyConfig(
                strategy_path=member.strategy_path,
                config_path=member.strategy_config_path,
                config={
                    **rewritten_config,
                    "manage_stop": True,
                    "order_id_tag": order_id_tag,
                    # Per-account topology is equities-only in PR 1.
                    # Apply the same US-equity TIF override the legacy
                    # portfolio builder applies so the IB account
                    # preset (DAY) matches the strategy-emitted TIF.
                    **_strategy_us_equity_tif_overrides(
                        rewritten_config,
                        extra_instruments=[
                            r.canonical_id for r in (member.resolved_instruments or ())
                        ],
                    ),
                },
            )
        )
    return strategy_configs


def build_redis_database_config() -> DatabaseConfig:
    """Build a Nautilus :class:`DatabaseConfig` for Redis bound
    to the project's ``REDIS_URL`` setting. Used by both:

    1. The live ``TradingNodeConfig`` writers (``CacheConfig`` +
       ``MessageBusConfig``) so the live subprocess writes
       through to Redis (Phase 3 tasks 3.1 + 3.2).
    2. The :class:`PositionReader` cold path (Phase 3 task 3.5)
       so the FastAPI process can read back from the same Redis
       keyspace the live subprocess writes to.

    Both call sites MUST use this helper — building a separate
    ``DatabaseConfig`` per call site would silently drop
    ``username`` / ``password`` / ``ssl`` on auth-protected or
    TLS-enabled Redis (Azure Cache for Redis, Upstash,
    ElastiCache). Codex batch 8 P1 — both writer and reader
    paths now share the same construction.

    Parses host, port, username, password, and TLS from
    ``settings.redis_url``. The URL form ``rediss://`` indicates
    TLS; ``redis://user:pass@host:port`` carries credentials.
    """
    parsed = urlparse(settings.redis_url)
    return DatabaseConfig(
        type="redis",
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        username=parsed.username,
        password=parsed.password,
        ssl=parsed.scheme == "rediss",
    )


class IBSettings(BaseModel):
    """Connection settings for the Interactive Brokers gateway.

    Held as a small value object (not env-var auto-loaded) so each
    deployment's builder call can pass its own settings — e.g. a future
    multi-account setup that runs paper and live nodes in parallel."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=4004)
    account_id: str = Field(default="DU0000000")


def _derive_data_client_id(deployment_slug: str) -> int:
    """Stable IB data-client id for the deployment.

    Thin wrapper around :func:`msai.services.nautilus.ibg_client_id.derive_ibg_client_id`
    kept here as a back-compat alias — callers in this module read more
    naturally with the "data" / "exec" intent in the function name than
    with a string-keyword argument.

    Codex iter 1 P1-5 of PR 1 (multi-account-broker-fleet) moved the raw
    derivation into the shared helper so the ``/api/v1/live/status``
    serializer (Task T14) can re-derive the SAME integer without reaching
    into the live-node config builder.
    """
    return derive_ibg_client_id(deployment_slug, ROLE_DATA)


def _derive_exec_client_id(deployment_slug: str) -> int:
    """Stable IB exec-client id for the deployment.

    Thin wrapper around :func:`msai.services.nautilus.ibg_client_id.derive_ibg_client_id`.
    See :func:`_derive_data_client_id` for the migration rationale.
    """
    return derive_ibg_client_id(deployment_slug, ROLE_EXEC)


def _derive_trader_id(deployment_slug: str) -> TraderId:
    """Build a Nautilus ``TraderId`` from the deployment slug.

    Format: ``MSAI-{deployment_slug}`` — matches the value persisted on
    ``LiveDeployment.trader_id`` by Task 1.1b so the live subprocess
    publishes state and message-bus events under the SAME identity the
    DB row tracks. A mismatch here silently breaks warm-restart state
    reload and the projection consumer's stream lookup (Codex Task 1.5
    iter2 P2 fix).
    """
    return TraderId(f"MSAI-{deployment_slug}")


def build_live_trading_node_config(
    *,
    deployment_slug: str,
    strategy_path: str,
    strategy_config_path: str,
    strategy_config: dict[str, Any],
    paper_symbols: list[str],
    ib_settings: IBSettings,
    max_notional_per_order: dict[str, int] | None = None,
    max_order_submit_rate: str = "100/00:00:01",
    max_order_modify_rate: str = "100/00:00:01",
    spawn_today: date | None = None,
) -> TradingNodeConfig:
    """Build the ``TradingNodeConfig`` for the live trading subprocess.

    Wires Nautilus's native engine + IB client configs and validates
    the IB Gateway port matches the account-id type.

    Args:
        deployment_slug: 16-char hex slug persisted on
            ``LiveDeployment.deployment_slug``. Drives the ``trader_id``
            and both ``ibg_client_id`` values so every id the live
            subprocess publishes resolves from the SAME source of truth
            the DB row tracks. Task 1.1b's stable-identity contract
            requires this alignment — a mismatch silently breaks
            warm-restart state reload and the projection consumer's
            stream lookup (Codex Task 1.5 iter2 P2 fix).
        strategy_path: Importable strategy class path, e.g.
            ``"strategies.example.ema_cross:EMACrossStrategy"``.
            Resolved by the live subprocess via Nautilus's
            ``StrategyFactory.create()``.
        strategy_config_path: Importable Nautilus ``StrategyConfig``
            (msgspec.Struct) class path that will be used to
            ``parse()`` ``strategy_config`` on the subprocess side,
            e.g. ``"strategies.example.config:EMACrossConfig"``. MUST
            point at a real ``NautilusConfig`` subclass — Nautilus's
            ``resolve_config_path()`` rejects anything else with
            ``TypeError`` (Codex Task 1.5 review P1 fix). The caller
            (Task 1.7 FleetRouter) is responsible for resolving the
            right config class for each strategy via the strategy
            registry.
        strategy_config: Strategy parameters (already validated /
            normalized by the API layer). Passed as the ``config`` field
            of ``ImportableStrategyConfig``; the subprocess parses this
            dict through ``strategy_config_path``'s class.
        paper_symbols: Phase 1 closed universe of symbols (e.g.
            ``["AAPL", "MSFT"]``). Resolved to IB contracts by the
            instrument bootstrap helper.
        ib_settings: IB Gateway connection + account settings.
        max_notional_per_order: Per-instrument cap on order
            notional value, enforced by Nautilus's built-in
            ``LiveRiskEngine`` (Task 3.8). Keys are canonical
            ``InstrumentId`` strings (e.g. ``"AAPL.NASDAQ"``);
            values are integer dollar caps. ``None`` (the
            default) installs no per-instrument cap — only the
            rate limits below apply. The custom checks
            (per-strategy max position, daily loss, kill
            switch, market hours) live in the
            :class:`RiskAwareStrategy` mixin from Task 3.7,
            NOT here.
        max_order_submit_rate: Native Nautilus rate limit for
            order submissions. Format is ``"<count>/<HH:MM:SS>"``
            (e.g. ``"100/00:00:01"`` = 100 per second). Default
            matches Nautilus's own default and is sized for
            real strategies; tests can override to ``"1/00:00:01"``
            to verify the throttle fires.
        max_order_modify_rate: Native Nautilus rate limit for
            order modifications. Same format as submit rate.

    Returns:
        A fully populated ``TradingNodeConfig`` ready to hand to
        ``TradingNode``.

    Raises:
        ValueError: For empty ``paper_symbols``, unknown port,
            paper-port-with-live-account, or live-port-with-paper-account.
    """
    if not paper_symbols:
        raise ValueError(
            "paper_symbols must contain at least one symbol — a TradingNode "
            "with no subscribed instruments cannot make progress."
        )
    # Normalize the account id ONCE and thread the normalized value through
    # both the validator and the exec client config. If we only strip inside
    # ``validate_port_account_consistency`` (Task 1.5 iter2 P2) but leave
    # the exec client to receive the raw ``ib_settings.account_id``, a value
    # like ``" DU1234567"`` from a misformatted ``.env`` passes validation
    # but reaches Nautilus with leading whitespace — IB Gateway then fails
    # the account match on connect (Codex batch 3 P2 fix).
    normalized_account_id = ib_settings.account_id.strip()
    validate_port_account_consistency(ib_settings.port, normalized_account_id)

    instrument_provider_config = build_ib_instrument_provider_config(
        paper_symbols,
        today=spawn_today,
    )
    data_client_id = _derive_data_client_id(deployment_slug)
    exec_client_id = _derive_exec_client_id(deployment_slug)

    # Map the string config value to the Nautilus enum.
    from nautilus_trader.adapters.interactive_brokers.config import (  # type: ignore[attr-defined]  # Nautilus 1.223 re-exports it but without __all__ entry
        IBMarketDataTypeEnum,
    )

    _mdt_map = {
        "REALTIME": IBMarketDataTypeEnum.REALTIME,
        "DELAYED": IBMarketDataTypeEnum.DELAYED,
        "DELAYED_FROZEN": IBMarketDataTypeEnum.DELAYED_FROZEN,
    }
    _mdt_str = settings.ib_market_data_type.upper()
    _market_data_type = _mdt_map.get(_mdt_str, IBMarketDataTypeEnum.REALTIME)

    # use_regular_trading_hours=False allows extended-hours data.
    # Required for FX (24h) and for equity strategies that need
    # after-hours bars. Without this, Nautilus filters bars older
    # than the RTH subscription start (market_data.py:1305).
    _use_rth = settings.ib_use_regular_trading_hours

    data_client = InteractiveBrokersDataClientConfig(
        ibg_host=ib_settings.host,
        ibg_port=ib_settings.port,
        ibg_client_id=data_client_id,
        instrument_provider=instrument_provider_config,
        market_data_type=_market_data_type,
        use_regular_trading_hours=_use_rth,
    )
    exec_client = InteractiveBrokersExecClientConfig(
        ibg_host=ib_settings.host,
        ibg_port=ib_settings.port,
        ibg_client_id=exec_client_id,
        account_id=normalized_account_id,
        instrument_provider=instrument_provider_config,
    )

    # Phase 3 tasks 3.1 + 3.2: live trading writes Cache + MessageBus
    # state through to Redis so a FastAPI restart can rehydrate the
    # projection layer without losing the running deployment's
    # positions / orders. Both configs share a single
    # :class:`DatabaseConfig` so they hit the same Redis instance.
    redis_database = build_redis_database_config()
    cache_config = CacheConfig(
        database=redis_database,
        encoding="msgpack",
        # Write-through (gotcha #7) — buffered pipelining loses up
        # to ``buffer_interval_ms`` of state on a crash. Codex #3
        # locked this to ``None`` (NOT ``0``, which Nautilus
        # rejects with a positive-int validation error).
        buffer_interval_ms=None,
        persist_account_events=True,
    )
    message_bus_config = MessageBusConfig(
        database=redis_database,
        encoding="msgpack",  # gotcha #17 — JSON fails on Decimal/datetime/Path
        # v3 decision #8 — ONE stream per trader, all topics
        # routed by the in-message ``topic`` field on the
        # consumer side. Wildcard XREADGROUP doesn't exist, so
        # ``stream_per_topic=True`` makes the stream names
        # un-discoverable from FastAPI.
        stream_per_topic=False,
        use_trader_prefix=True,
        use_trader_id=True,
        streams_prefix="stream",
        buffer_interval_ms=None,  # Codex #3 — write-through
    )

    return TradingNodeConfig(
        trader_id=_derive_trader_id(deployment_slug),
        # Phase 4 task 4.1: enable Nautilus's built-in state
        # persistence so a restarted subprocess can pick up
        # exactly where the previous one left off.
        # ``load_state`` and ``save_state`` BOTH default to
        # False on TradingNodeConfig (system/config.py:122-123)
        # despite the docstring saying True — Codex gotcha #10.
        # Forgetting to flip them is the silent path to a
        # restart that quietly resets every strategy's
        # internal state (EMA values, position tracking,
        # etc.) to first-bar defaults.
        load_state=True,
        save_state=True,
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(
            # Phase 1: enable startup reconciliation against
            # IB so the trader picks up any orders / fills
            # that landed while it was offline.
            reconciliation=True,
            reconciliation_lookback_mins=1440,
            # Phase 4 task 4.1: keep Nautilus's in-flight
            # order watchdog active. Defaults match Nautilus
            # 1.223.0 (live/config.py:202-204) but we set them
            # explicitly so a future Nautilus default change
            # doesn't silently relax our checks.
            inflight_check_interval_ms=2000,
            inflight_check_threshold_ms=5000,
            # Periodic position reconciliation against the
            # broker — catches any position drift that the
            # event-driven path missed.
            position_check_interval_secs=60,
        ),
        risk_engine=LiveRiskEngineConfig(
            # Phase 3 task 3.8: real native limits.
            # bypass=False ensures every order goes through
            # the engine. The submit/modify rate limits cap
            # accidental order storms (e.g. a strategy bug
            # firing 10k orders/sec). max_notional_per_order
            # is the LAST native check before the order goes
            # to IB — combined with the RiskAwareStrategy
            # mixin's pre-submit checks (3.7), we get
            # defense-in-depth on every order.
            bypass=False,
            max_order_submit_rate=max_order_submit_rate,
            max_order_modify_rate=max_order_modify_rate,
            max_notional_per_order=max_notional_per_order or {},
        ),
        cache=cache_config,
        message_bus=message_bus_config,
        data_clients={IB_VENUE.value: data_client},
        exec_clients={IB_VENUE.value: exec_client},
        strategies=[
            ImportableStrategyConfig(
                strategy_path=strategy_path,
                config_path=strategy_config_path,
                # Phase 1 task 1.10: inject two fields on top of the
                # caller's config before handing it to Nautilus.
                #
                #   - ``manage_stop=True`` enables Nautilus's built-in
                #     market-exit loop on strategy stop: cancels open
                #     orders and submits market orders to flatten
                #     positions (``trading/strategy.pyx:1779``). v2
                #     had a custom ``on_stop`` that did this by hand;
                #     v3+ uses the native path per gotcha #13.
                #
                #   - ``order_id_tag=deployment_slug`` makes every
                #     ``client_order_id`` Nautilus mints on this
                #     strategy prefix-stable across restarts. Decision
                #     #7 makes the slug the one stable identifier;
                #     threading it through the order-id tag is what
                #     lets the audit hook (Task 1.11) correlate
                #     orders to a deployment deterministically.
                config={
                    **strategy_config,
                    "manage_stop": True,
                    # Include order_index=0 so Nautilus emits
                    # ``{class}-0-{slug}`` which matches the format
                    # ``derive_strategy_id_full(class, slug, 0)``
                    # produces. Without the ``0-`` prefix the
                    # StrategyId and strategy_id_full would diverge.
                    "order_id_tag": f"0-{deployment_slug}",
                    # US-equity venues: override Nautilus's default GTC
                    # to match IB account preset DAY (Bug #2 fix).
                    **_strategy_us_equity_tif_overrides(strategy_config),
                },
            ),
        ],
    )


def build_portfolio_trading_node_config(
    *,
    deployment_slug: str,
    strategy_members: list[StrategyMemberPayload],
    ib_settings: IBSettings,
    max_notional_per_order: dict[str, int] | None = None,
    max_order_submit_rate: str = "100/00:00:01",
    max_order_modify_rate: str = "100/00:00:01",
    spawn_today: date | None = None,
) -> TradingNodeConfig:
    """Build a ``TradingNodeConfig`` for a multi-strategy portfolio deployment.

    Like :func:`build_live_trading_node_config` but accepts N strategy
    members instead of one, building N ``ImportableStrategyConfig`` objects
    that share a SINGLE IB exec/data client and a single instrument
    provider covering ALL members' instruments.

    Key differences from the single-strategy builder:

    - ``strategies`` is a list of length N (one per member)
    - ``load_state=True, save_state=True`` always — critical for warm
      restart of the portfolio
    - Instruments are aggregated across ALL members for the provider config
    - Each strategy's ``order_id_tag`` uses ``strategy_id_full`` (not the
      deployment_slug) so orders are attributable to individual strategies

    Args:
        deployment_slug: 16-char hex slug. Drives trader_id and IB client ids.
        strategy_members: One or more strategy payloads. Must be non-empty.
        ib_settings: IB Gateway connection + account settings.
        max_notional_per_order: Per-instrument cap on order notional value.
        max_order_submit_rate: Nautilus rate limit for order submissions.
        max_order_modify_rate: Nautilus rate limit for order modifications.
        spawn_today: Exchange-local date for front-month futures resolution.

    Returns:
        A fully populated ``TradingNodeConfig`` ready for ``TradingNode``.

    Raises:
        ValueError: For empty ``strategy_members``, no instruments across
            all members, unknown port, or port/account mismatch.
    """
    if not strategy_members:
        raise ValueError(
            "strategy_members must contain at least one member — a portfolio "
            "deployment with no strategies cannot make progress."
        )

    # Aggregate bare-symbol instruments across all members (de-duped).
    # This check preserves the original "no instruments" fail-fast for
    # StrategyMemberPayload construction errors; the IB provider config
    # itself is now built from ``resolved_instruments`` below (Task 11 —
    # registry-backed path). ``spawn_today`` is no longer consumed here
    # because the resolver (``lookup_for_live``) owns futures rollover
    # before the payload ever reaches the config builder.
    _ = spawn_today  # Retained in signature for supervisor call-site stability.
    all_instruments: set[str] = set()
    for member in strategy_members:
        all_instruments.update(member.instruments)
    if not all_instruments:
        raise ValueError(
            "No instruments found across all strategy_members — a TradingNode "
            "with no subscribed instruments cannot make progress."
        )

    normalized_account_id = ib_settings.account_id.strip()
    validate_port_account_consistency(ib_settings.port, normalized_account_id)

    # Aggregate ResolvedInstrument across all members, deduped by
    # canonical_id so two strategies subscribing to the same instrument
    # produce one IBContract, not two. The dedup is first-wins — the
    # resolver (single source of truth) guarantees canonical_id
    # uniqueness within a spawn, so "first wins" never discards a
    # different spec.
    seen: dict[str, ResolvedInstrument] = {}
    for member in strategy_members:
        for ri in member.resolved_instruments:
            seen.setdefault(ri.canonical_id, ri)
    aggregated = list(seen.values())

    if not aggregated:
        raise ValueError(
            "No resolved_instruments found across strategy_members — "
            "supervisor must thread lookup_for_live output through "
            "StrategyMemberPayload.resolved_instruments (see Task 9)."
        )

    instrument_provider_config = build_ib_instrument_provider_config_from_resolved(
        aggregated,
    )
    data_client_id = _derive_data_client_id(deployment_slug)
    exec_client_id = _derive_exec_client_id(deployment_slug)

    # Map the string config value to the Nautilus enum.
    from nautilus_trader.adapters.interactive_brokers.config import (  # type: ignore[attr-defined]  # Nautilus 1.223 re-exports it but without __all__ entry
        IBMarketDataTypeEnum,
    )

    _mdt_map = {
        "REALTIME": IBMarketDataTypeEnum.REALTIME,
        "DELAYED": IBMarketDataTypeEnum.DELAYED,
        "DELAYED_FROZEN": IBMarketDataTypeEnum.DELAYED_FROZEN,
    }
    _mdt_str = settings.ib_market_data_type.upper()
    _market_data_type = _mdt_map.get(_mdt_str, IBMarketDataTypeEnum.REALTIME)
    _use_rth = settings.ib_use_regular_trading_hours

    data_client = InteractiveBrokersDataClientConfig(
        ibg_host=ib_settings.host,
        ibg_port=ib_settings.port,
        ibg_client_id=data_client_id,
        instrument_provider=instrument_provider_config,
        market_data_type=_market_data_type,
        use_regular_trading_hours=_use_rth,
    )
    exec_client = InteractiveBrokersExecClientConfig(
        ibg_host=ib_settings.host,
        ibg_port=ib_settings.port,
        ibg_client_id=exec_client_id,
        account_id=normalized_account_id,
        instrument_provider=instrument_provider_config,
    )

    redis_database = build_redis_database_config()
    cache_config = CacheConfig(
        database=redis_database,
        encoding="msgpack",
        buffer_interval_ms=None,
        persist_account_events=True,
    )
    message_bus_config = MessageBusConfig(
        database=redis_database,
        encoding="msgpack",
        stream_per_topic=False,
        use_trader_prefix=True,
        use_trader_id=True,
        streams_prefix="stream",
        buffer_interval_ms=None,
    )

    # Build N ImportableStrategyConfigs — one per member.
    # Each strategy's order_id_tag is the SUFFIX of strategy_id_full
    # (without the class name). Nautilus constructs StrategyId as
    # ``f"{class_name}-{order_id_tag}"``, so if strategy_id_full is
    # ``"EMACross-0-slug"`` the tag must be ``"0-slug"`` — otherwise
    # Nautilus would produce ``"EMACross-EMACross-0-slug"`` (double
    # prefix).
    strategy_configs: list[ImportableStrategyConfig] = []
    for member in strategy_members:
        # Parse "{class}-{order_index}-{slug}" → "{order_index}-{slug}"
        _parts = member.strategy_id_full.split("-", 1)
        order_id_tag = _parts[1] if len(_parts) >= 2 else deployment_slug
        strategy_configs.append(
            ImportableStrategyConfig(
                strategy_path=member.strategy_path,
                config_path=member.strategy_config_path,
                config={
                    **member.strategy_config,
                    "manage_stop": True,
                    "order_id_tag": order_id_tag,
                    # US-equity venues: override Nautilus default GTC
                    # to match IB account preset DAY (Bug #2 fix).
                    # PR #65 Codex P2 round-3: use `resolved_instruments`
                    # — those carry the canonical "SYMBOL.VENUE" form
                    # (e.g. "AAPL.NASDAQ") that `_has_us_equity_venue`
                    # parses. `member.instruments` carries paper roots
                    # ("AAPL" via `inst.split(".")[0]` in the payload
                    # factory) which have no `.VENUE` suffix → would
                    # never trigger the override.
                    **_strategy_us_equity_tif_overrides(
                        member.strategy_config,
                        extra_instruments=[
                            r.canonical_id for r in (member.resolved_instruments or ())
                        ],
                    ),
                },
            ),
        )

    return TradingNodeConfig(
        trader_id=_derive_trader_id(deployment_slug),
        load_state=True,
        save_state=True,
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=1440,
            inflight_check_interval_ms=2000,
            inflight_check_threshold_ms=5000,
            position_check_interval_secs=60,
        ),
        risk_engine=LiveRiskEngineConfig(
            bypass=False,
            max_order_submit_rate=max_order_submit_rate,
            max_order_modify_rate=max_order_modify_rate,
            max_notional_per_order=max_notional_per_order or {},
        ),
        cache=cache_config,
        message_bus=message_bus_config,
        data_clients={IB_VENUE.value: data_client},
        exec_clients={IB_VENUE.value: exec_client},
        strategies=strategy_configs,
    )


def build_per_account_trading_node_config(
    *,
    account_id: str,
    ibg_client_id: int,
    ib_login_key: str,
    native_instrument_ids: list[str],
    venue_dataset_map: dict[str, str],
    canonical_to_native_bar_types: dict[str, str],
    databento_api_key: str,
    ib_host: str,
    ib_port: int,
    deployment_slug: str | None = None,
    strategies: list[ImportableStrategyConfig] | None = None,
    resolved_instruments: list[ResolvedInstrument] | None = None,
    max_notional_per_order: dict[str, int] | None = None,
    max_order_submit_rate: str = "100/00:00:01",
    max_order_modify_rate: str = "100/00:00:01",
) -> TradingNodeConfig:
    """Per-account ``TradingNodeConfig`` — Databento data + IB exec only.

    Builds the SYNC ``TradingNodeConfig`` for the multi-account broker
    fleet (PR 1 Task T11). The shape diverges from the legacy
    :func:`build_live_trading_node_config` / :func:`build_portfolio_trading_node_config`
    builders in three ways:

    1. **No IB data client.** ``data_clients`` contains exactly one
       entry — the Databento client keyed under
       ``str(DATABENTO_CLIENT_ID)`` (the Nautilus-canonical client
       name). Asserted at build time.
    2. **One IB exec client per account.** ``exec_clients[IB_VENUE.value]``
       carries the per-account ``account_id`` + ``ibg_client_id``. The
       supervisor's payload factory (Task T12) is responsible for
       allocating distinct ``ibg_client_id`` values per concurrent
       account so IB Gateway's gotcha #3 (silent disconnect on
       collision) can't fire.
    3. **A ``SymbologyShimActor`` registered via ``ImportableActorConfig``.**
       The actor's ``on_start`` subscribes to NATIVE Databento bar types
       (driven by ``canonical_to_native_bar_types``) so the data engine
       routes those subscriptions to the Databento client. When native
       bars arrive on the bus, the actor retags them to the canonical
       ``.IBKR`` venue and republishes for the strategy's bus
       subscription to pick up. Without BOTH halves wired (outbound
       subscription + inbound retag) no bars ever reach the strategy
       (Codex iter 5–7 of PR 1).

    Args:
        account_id: IB account id this node executes against (e.g.
            ``"DUP733214"`` for a paper account, ``"U..."`` for live).
        ibg_client_id: IB Gateway client_id slot to claim for the exec
            connection. Caller (supervisor) MUST ensure distinct values
            per concurrent account.
        ib_login_key: IB username/login key that the Gateway container
            is bound to. Currently only embedded for audit; surfaced
            here for symmetry with the supervisor's payload factory.
        native_instrument_ids: Pre-resolved Databento native ids
            (e.g. ``["AAPL.XNAS"]``). Resolution happens upstream in
            the supervisor's async payload factory; this builder is
            sync and MUST NOT call any async DB code.
        venue_dataset_map: Pre-resolved native_venue → dataset map
            (e.g. ``{"XNAS": "EQUS.MINI"}``). Passed straight to the
            Databento client config so Nautilus uses our authoritative
            choice rather than publisher-default lookup.
        canonical_to_native_bar_types: Pre-built mapping from canonical
            ``<SYM>.IBKR-1-MINUTE-LAST-EXTERNAL`` to native
            ``<SYM>.XNAS-1-MINUTE-LAST-EXTERNAL``. Threaded into the
            ``SymbologyShimActor`` config so ``on_start`` knows which
            native bar types to subscribe to. Empty dict is permitted
            for tests that only exercise the data/exec topology.
        databento_api_key: Databento live-API key the data client uses.
        ib_host: IB Gateway hostname (DNS or IP).
        ib_port: IB Gateway port (4002 = paper, 4001 = live; the
            host-side socat proxy may use 4004 / 4003 — pass whichever
            the supervisor was configured with).
        deployment_slug: Optional 16-char hex slug to drive the
            ``trader_id``. ``None`` falls back to a synthetic slug
            derived from ``account_id`` so callers that only have the
            account id (e.g. unit tests, PR 1 T14 status preview) can
            still construct a config. Real supervisor calls always pass
            the real slug.
        strategies: Optional pre-built ``ImportableStrategyConfig`` list.
            PR 1 doesn't define the wire-up of strategies through the
            new builder — the supervisor (PR 2's per-account ownership
            refactor) will assemble these — so the default is an empty
            list. The shape is preserved so the field is forward-compatible.
        max_notional_per_order: Per-instrument cap on order notional.
            Same semantics as the legacy builders.
        max_order_submit_rate: Native Nautilus order-submission throttle.
        max_order_modify_rate: Native Nautilus order-modification throttle.

    Returns:
        A fully populated ``TradingNodeConfig`` with the per-account
        Databento + IB exec topology. The caller (the subprocess's
        ``node_factory``) is responsible for registering BOTH the
        Databento data-client factory AND the IB exec-client factory
        with the ``TradingNode`` before ``node.build()``.

    Raises:
        ValueError: ``account_id`` is empty, port/account mismatch
            (gotcha #6), or the IB venue key would collide with the
            Databento client key.
    """
    # Defer-import locally so this module's import cost stays low and
    # so the new builder doesn't accidentally couple the legacy paths
    # to the Databento adapter at import time.
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID
    from nautilus_trader.common.config import ImportableActorConfig

    if not account_id or not account_id.strip():
        raise ValueError(
            "build_per_account_trading_node_config requires a non-empty account_id; "
            "the supervisor's payload factory MUST resolve the account from the "
            "broker-account registry before reaching this builder."
        )

    normalized_account_id = account_id.strip()
    validate_port_account_consistency(ib_port, normalized_account_id)

    # The trader_id needs SOME stable identifier. Real callers pass a
    # 16-hex-char deployment_slug; tests that only care about the
    # client-topology shape may omit it, in which case we synthesize
    # something from the account id (still deterministic). NEVER fall
    # back to a random id — that would make warm-restart state reload
    # non-deterministic across process restarts.
    effective_slug = deployment_slug or f"acct-{normalized_account_id}"
    trader_id = TraderId(f"MSAI-{effective_slug}")

    # --- Databento data client (Codex iter 4 P2-2) ----------------------
    # ``build_databento_data_client_config`` is the SYNC half of T10's
    # split builder. Resolution of canonical → native already happened
    # in the supervisor; this call is pure config assembly.
    data_client = build_databento_data_client_config(
        native_instrument_ids=native_instrument_ids,
        venue_dataset_map=venue_dataset_map,
        api_key=databento_api_key,
    )

    # --- IB exec instrument provider (Codex iter 2 P1 fix — F2) ---------
    # The IB exec adapter resolves contracts at order-submit time via
    # its ``instrument_provider``. Without preloaded contracts the
    # lookup hits IB synchronously on the critical path, and a contract
    # that isn't already in cache fails with "no instrument found" —
    # see ``nautilus.md`` gotcha #9 (instrument not pre-loaded fails at
    # runtime, not startup) and gotcha #11 (dynamic loading is slow).
    #
    # **F2 fix (Codex iter 2):** the prior implementation passed
    # ``load_ids=frozenset(InstrumentId("AAPL.IBKR"), ...)`` via
    # ``SymbologyMethod.IB_SIMPLIFIED``. Under IB_SIMPLIFIED the
    # InstrumentId.venue is interpreted as the IB exchange / primary
    # exchange during contract resolution — but ``IBKR`` is NOT a valid
    # IB exchange/MIC, so the preload silently failed and the first
    # order on a per-account equity strategy hit unresolved contracts.
    #
    # Mirror the legacy ``build_portfolio_trading_node_config`` path:
    # preload via ``load_contracts`` populated with real ``IBContract``
    # objects derived from the resolver's ``ResolvedInstrument``
    # ``contract_spec`` dicts (which carry the LISTING venue —
    # NASDAQ/NYSE/etc. — as ``primaryExchange``). This is the SAME
    # mechanism the working portfolio builder uses today, so the per-
    # account topology now inherits the legacy preload's correctness
    # by construction.
    ib_instrument_provider_config = build_ib_instrument_provider_config_from_resolved(
        list(resolved_instruments or [])
    )

    # --- IB exec-only client --------------------------------------------
    # PR 1 T11 explicitly drops the IB data client. Data flows via
    # Databento; IB only executes orders + reports fills + serves as the
    # account-state source of truth. Reconciliation against IB is still
    # enabled via the LiveExecEngineConfig below.
    exec_client = InteractiveBrokersExecClientConfig(
        ibg_host=ib_host,
        ibg_port=ib_port,
        ibg_client_id=ibg_client_id,
        account_id=normalized_account_id,
        # Preload the canonical ``.IBKR`` instrument ids the strategies
        # will subscribe to + place orders on. Without this the IB exec
        # adapter's contract qualification path runs at submit time —
        # the first order on AAPL.IBKR would fail before the provider
        # ever reaches IB Gateway. Codex iter 1 P1-2 fix.
        instrument_provider=ib_instrument_provider_config,
    )

    # --- Redis-backed cache + message bus (gotcha #7, #8) ---------------
    redis_database = build_redis_database_config()
    cache_config = CacheConfig(
        database=redis_database,
        encoding="msgpack",
        buffer_interval_ms=None,  # write-through
        persist_account_events=True,
    )
    message_bus_config = MessageBusConfig(
        database=redis_database,
        encoding="msgpack",
        stream_per_topic=False,
        use_trader_prefix=True,
        use_trader_id=True,
        streams_prefix="stream",
        buffer_interval_ms=None,
    )

    # --- Symbology shim actor (Codex iter 7 P1) -------------------------
    # The actor's two-way bridge is THE load-bearing piece of T11:
    # outbound subscribes native bar types so Databento streams them;
    # inbound retags + republishes onto the canonical .IBKR topic for
    # the strategy's bus subscription. Both halves are required for
    # bars to reach the strategy.
    shim_actor_config = ImportableActorConfig(
        actor_path="msai.services.symbology_shim_actor:SymbologyShimActor",
        config_path="msai.services.symbology_shim_actor:SymbologyShimActorConfig",
        config={
            "canonical_to_native_bar_types": dict(canonical_to_native_bar_types),
            "venue_dataset_map": dict(venue_dataset_map),
            # ib_login_key surfaced into the actor config for audit
            # symmetry; the actor itself ignores it today.
            "ib_login_key": ib_login_key,
        },
    )

    # --- Data-freshness observer actor (PR 1b Task 2) -------------------
    # A SEPARATE actor that subscribes to the same NATIVE Databento bar
    # types the shim already streams and records each bar's event/arrival
    # timestamps into a shared FreshnessRegistry (injected post-build by
    # the subprocess). Subscribing to an already-subscribed EXTERNAL bar
    # type is a free observation tap — no duplicate upstream Databento
    # subscription (engine.pyx:1191 + msgbus fan-out).
    #
    # bar_type_datasets is DERIVED ONCE here from the canonical→native map
    # + venue_dataset_map: each native bar-type string's venue suffix
    # (``<SYM>.<VENUE>-...``) keys into venue_dataset_map for its dataset.
    # Gate on the same data the shim needs — empty canonical map means the
    # legacy path, so no freshness actor is added.
    actors: list[ImportableActorConfig] = [shim_actor_config]
    if canonical_to_native_bar_types:
        # Defer-import BarType so this module's top-level import cost stays low
        # (and the uvloop-policy install at nautilus import time stays deferred,
        # gotcha #1). BarType.from_str is the AUTHORITATIVE venue parser: a hand
        # ``split('.', 1)[-1]`` mis-parses dotted share-class symbols like
        # ``BRK.B.XNYS-...`` (venue would be ``B.XNYS``), missing
        # venue_dataset_map and SILENTLY dropping the feed from the freshness
        # manifest — no auto-halt for that feed.
        from nautilus_trader.model.data import BarType

        native_bar_types = list(canonical_to_native_bar_types.values())
        bar_type_datasets: dict[str, str] = {}
        for native_str in native_bar_types:
            venue = BarType.from_str(native_str).instrument_id.venue.value
            dataset = venue_dataset_map.get(venue)
            if dataset is None:
                # FAIL CLOSED: a required feed that cannot be mapped to a
                # Databento dataset must fail the deploy loudly, not lose its
                # freshness protection silently.
                raise ValueError(
                    "build_per_account_trading_node_config: native bar type "
                    f"{native_str!r} resolves to venue {venue!r}, which has no "
                    f"entry in venue_dataset_map ({sorted(venue_dataset_map)}); "
                    "the data-stale monitor cannot protect a feed it cannot map "
                    "to a dataset."
                )
            bar_type_datasets[native_str] = dataset
        freshness_actor_config = ImportableActorConfig(
            actor_path="msai.services.nautilus.data_freshness_actor:DataFreshnessActor",
            config_path="msai.services.nautilus.data_freshness_actor:DataFreshnessActorConfig",
            config={
                "native_bar_types": native_bar_types,
                "bar_type_datasets": bar_type_datasets,
            },
        )
        actors.append(freshness_actor_config)

    # --- Assemble the TradingNodeConfig --------------------------------
    config = TradingNodeConfig(
        trader_id=trader_id,
        load_state=True,
        save_state=True,
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=1440,
            inflight_check_interval_ms=2000,
            inflight_check_threshold_ms=5000,
            position_check_interval_secs=60,
        ),
        risk_engine=LiveRiskEngineConfig(
            bypass=False,
            max_order_submit_rate=max_order_submit_rate,
            max_order_modify_rate=max_order_modify_rate,
            max_notional_per_order=max_notional_per_order or {},
        ),
        cache=cache_config,
        message_bus=message_bus_config,
        # Databento OWNS the data side — keyed by the Nautilus-canonical
        # client name (the str-cast of DATABENTO_CLIENT_ID, which is
        # itself a ClientId("DATABENTO")). The subprocess registers
        # DatabentoLiveDataClientFactory against this same name.
        data_clients={str(DATABENTO_CLIENT_ID): data_client},
        # IB OWNS exec only. Keyed by IB_VENUE.value ("INTERACTIVE_BROKERS")
        # for symmetry with the legacy builders (existing tests at
        # tests/unit/test_live_node_config.py:278 already assert this).
        exec_clients={IB_VENUE.value: exec_client},
        actors=actors,
        strategies=strategies or [],
    )

    # Build-time guard: assert no IB data client snuck in. PR 1 T11's
    # architectural invariant is "Databento owns data; IB owns exec".
    if IB_VENUE.value in config.data_clients:
        raise ValueError(
            "build_per_account_trading_node_config: invariant violated — "
            "IB data client wired into data_clients. PR 1 T11 mandates "
            "Databento as the sole data source; IB Gateway is exec-only."
        )

    return config
