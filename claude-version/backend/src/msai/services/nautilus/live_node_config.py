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
from typing import Any
from urllib.parse import urlparse

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
from msai.services.nautilus.live_instrument_bootstrap import (
    build_ib_instrument_provider_config,
)

# IB Gateway listens on:
#   4001 — live trading
#   4002 — paper trading
# Both with the same wire protocol, distinguished only by which account
# the gateway is logged into. Mismatching the port and account id is
# the silent failure mode gotcha #6 catches.
_IB_PAPER_PORT = 4002
_IB_LIVE_PORT = 4001
_IB_PAPER_PREFIX = "DU"  # IB paper-account ids start with "DU"


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
    port: int = Field(default=_IB_PAPER_PORT)
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


def _validate_port_account_consistency(port: int, account_id: str) -> None:
    """Reject silent gotcha-#6 misconfigurations at build time.

    - 4002 + non-DU account → "paper port + live account"
    - 4001 + DU account     → "live port + paper account"
    - any other port        → "unsupported IB Gateway port"
    - blank/whitespace-only account_id → rejected before classification

    Account ids are stripped of surrounding whitespace before
    classification so a value like ``' DU1234567'`` from a misformatted
    ``.env`` file isn't misclassified as a live account (Codex Task
    1.5 iteration 2 P2 fix).
    """
    normalized_account = account_id.strip()
    if not normalized_account:
        raise ValueError(
            "IB account id is empty (or whitespace only) — set IB_ACCOUNT_ID "
            "to a real paper or live account id before starting a deployment."
        )
    is_paper_account = normalized_account.startswith(_IB_PAPER_PREFIX)
    if port == _IB_PAPER_PORT:
        if not is_paper_account:
            raise ValueError(
                f"IB paper port {port} requires a paper account id (starts with "
                f"'{_IB_PAPER_PREFIX}'); got live account {normalized_account!r}. "
                "This combination silently produces no data — see Nautilus gotcha #6."
            )
    elif port == _IB_LIVE_PORT:
        if is_paper_account:
            raise ValueError(
                f"IB live port {port} requires a live account id (must NOT start "
                f"with '{_IB_PAPER_PREFIX}'); got paper account {normalized_account!r}. "
                "This combination silently produces no data — see Nautilus gotcha #6."
            )
    else:
        raise ValueError(
            f"unsupported IB Gateway port {port}: only {_IB_LIVE_PORT} (live) "
            f"and {_IB_PAPER_PORT} (paper) are recognized."
        )


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
    # ``_validate_port_account_consistency`` (Task 1.5 iter2 P2) but leave
    # the exec client to receive the raw ``ib_settings.account_id``, a value
    # like ``" DU1234567"`` from a misformatted ``.env`` passes validation
    # but reaches Nautilus with leading whitespace — IB Gateway then fails
    # the account match on connect (Codex batch 3 P2 fix).
    normalized_account_id = ib_settings.account_id.strip()
    _validate_port_account_consistency(ib_settings.port, normalized_account_id)

    instrument_provider_config = build_ib_instrument_provider_config(paper_symbols)
    data_client_id = _derive_data_client_id(deployment_slug)
    exec_client_id = _derive_exec_client_id(deployment_slug)

    data_client = InteractiveBrokersDataClientConfig(
        ibg_host=ib_settings.host,
        ibg_port=ib_settings.port,
        ibg_client_id=data_client_id,
        instrument_provider=instrument_provider_config,
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
                    "order_id_tag": deployment_slug,
                },
            ),
        ],
    )
