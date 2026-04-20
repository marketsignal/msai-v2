"""Phase 1 live ``TradingNodeConfig`` builder.

Constructs the Nautilus ``TradingNodeConfig`` that the live trading
subprocess hands to ``TradingNode``. Uses Nautilus natives for every
engine and client config so we get reconciliation, risk checks, and IB
integration "for free" (decision: don't reinvent what Nautilus already
provides — see the natives audit).

Phase 1 deliberately leaves a few things at default that later phases
fill in:

- ``cache.database`` and ``message_bus.database`` stay None — Phase 3
  task 3.2 wires Redis as the durable backend. Phase 1 runs in-memory.
- ``load_state`` and ``save_state`` are False — Phase 4 task 4.5
  enables them once the persistence path has been smoke-tested.
- ``message_bus`` does not yet pin a stream name — Phase 3 task 3.2
  sets ``stream_per_topic=False`` and the deployment-specific stream.

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

import hashlib
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
from nautilus_trader.model.identifiers import TraderId
from pydantic import BaseModel, Field

from msai.core.config import settings
from msai.services.nautilus.ib_port_validator import (
    validate_port_account_consistency,
)
from msai.services.nautilus.live_instrument_bootstrap import (
    build_ib_instrument_provider_config,
    build_ib_instrument_provider_config_from_resolved,
)


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


def _derive_client_id(deployment_slug: str, role: str) -> int:
    """Stable 31-bit positive integer derived from the deployment slug + role.

    IB ``client_id`` is a signed 32-bit int; we mask to 31 bits to
    avoid the high bit (some IB middleware doesn't like negative ids).

    Determinism matters: the same ``(deployment_slug, role)`` pair must
    always produce the same id so a restart reconnects under the SAME
    client identity — otherwise IB Gateway sees a "new" connection and
    the old client's open orders + subscriptions get stranded.

    The ``role`` salt (``"data"`` or ``"exec"``) is mixed in via sha256
    so two clients on the same deployment can never collide regardless
    of slug structure (gotcha #3).

    Zero is mapped to 1 because IB Gateway treats client_id=0 as a
    privileged "master" connection — we never want to claim that slot
    by accident.

    We key on the ``deployment_slug`` (not the UUID primary key) so
    every id the live subprocess publishes — ``trader_id``,
    ``ibg_client_id``, ``message_bus_stream`` — resolves from the SAME
    16-hex-char source of truth persisted on ``LiveDeployment.deployment_slug``
    (Codex Task 1.5 iter2 P2 fix: Task 1.1b's stable-identity contract).
    """
    digest = hashlib.sha256(deployment_slug.encode("ascii") + role.encode("ascii")).digest()
    raw = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return raw or 1


def _derive_data_client_id(deployment_slug: str) -> int:
    return _derive_client_id(deployment_slug, "data")


def _derive_exec_client_id(deployment_slug: str) -> int:
    return _derive_client_id(deployment_slug, "exec")


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
            (Task 1.7 ProcessManager) is responsible for resolving the
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
    from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum

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
    from nautilus_trader.adapters.interactive_brokers.config import IBMarketDataTypeEnum

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
