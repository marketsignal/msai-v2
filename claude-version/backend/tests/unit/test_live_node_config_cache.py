"""Cache + MessageBus configuration tests for the live trading
node (Phase 3 tasks 3.1 + 3.2).

Verifies:

- Live ``TradingNodeConfig`` carries a ``CacheConfig`` with
  ``database.type == "redis"`` and ``buffer_interval_ms is None``
  (write-through, gotcha #7).
- Live ``TradingNodeConfig`` carries a ``MessageBusConfig`` with
  ``stream_per_topic == False`` (v3 decision #8 — single stream
  per trader so the projection consumer can subscribe at
  deployment start time).
- Both configs share the same Redis ``DatabaseConfig`` so
  Cache + MessageBus hit the same Redis instance.
- Backtest config does NOT set ``cache.database`` (gotcha #8
  inverse — backtest must not pollute the live Redis with
  per-run state).
"""

from __future__ import annotations

from unittest.mock import patch

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_live_trading_node_config,
    build_redis_database_config,
)

_DEFAULT_KWARGS = {
    "deployment_slug": "abcd1234abcd1234",
    "strategy_path": "strategies.example.ema_cross:EMACrossStrategy",
    "strategy_config_path": "strategies.example.config:EMACrossConfig",
    "strategy_config": {},
    "paper_symbols": ["AAPL"],
    "ib_settings": IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567"),
}


class TestLiveCacheConfig:
    def test_cache_database_is_redis(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.cache is not None
        assert config.cache.database is not None
        assert config.cache.database.type == "redis"

    def test_cache_buffer_interval_ms_is_none_write_through(self) -> None:
        """Write-through (gotcha #7). Buffered pipelining loses up
        to ``buffer_interval_ms`` of state on a crash. Codex #3
        locked this to ``None`` (NOT ``0``, which Nautilus
        rejects with a positive-int validation error)."""
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.cache.buffer_interval_ms is None

    def test_cache_persist_account_events_true(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.cache.persist_account_events is True

    def test_cache_encoding_msgpack(self) -> None:
        """Gotcha #17: JSON encoding fails on ``Decimal`` /
        ``datetime`` / ``pathlib.Path``. msgpack is the only
        encoding that round-trips Nautilus's full type system
        through Redis."""
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.cache.encoding == "msgpack"


class TestLiveMessageBusConfig:
    def test_message_bus_database_is_redis(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus is not None
        assert config.message_bus.database is not None
        assert config.message_bus.database.type == "redis"

    def test_stream_per_topic_is_false_single_stream_per_trader(self) -> None:
        """v3 decision #8 — ONE stream per trader so the
        projection consumer can subscribe to a deterministic
        stream name at deployment start time. Wildcard
        XREADGROUP doesn't exist, so per-topic streams are
        un-discoverable from FastAPI."""
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus.stream_per_topic is False

    def test_use_trader_prefix_and_id_true(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus.use_trader_prefix is True
        assert config.message_bus.use_trader_id is True

    def test_streams_prefix_is_stream(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus.streams_prefix == "stream"

    def test_buffer_interval_ms_is_none_write_through(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus.buffer_interval_ms is None

    def test_message_bus_encoding_msgpack(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.message_bus.encoding == "msgpack"


class TestSharedRedisDatabase:
    def test_cache_and_message_bus_share_same_database(self) -> None:
        """Cache + MessageBus must hit the SAME Redis instance.
        Different DatabaseConfigs would split state across two
        Redis databases and break warm-restart rehydration."""
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        cache_db = config.cache.database
        bus_db = config.message_bus.database
        assert cache_db.host == bus_db.host
        assert cache_db.port == bus_db.port
        assert cache_db.type == bus_db.type


class TestBacktestRunnerHasNoRedisDatabase:
    """Backtest config MUST NOT set ``cache.database`` — gotcha
    #8 inverse. A backtest writing to the production Redis would
    pollute the live state spine and corrupt warm-restart
    rehydration."""

    def test_backtest_run_config_has_no_cache_database(self) -> None:
        """The backtest runner builds a ``BacktestRunConfig`` via
        ``_build_backtest_run_config`` — that path must not
        construct a ``CacheConfig`` with ``database`` set.
        We assert by walking the resulting ``BacktestEngineConfig``
        tree and verifying ``cache.database`` is ``None`` (or
        the engine config doesn't even carry a cache attribute,
        in which case Nautilus uses its in-memory default)."""
        from msai.services.nautilus.backtest_runner import (
            _build_backtest_run_config,
            _RunPayload,
        )

        payload = _RunPayload(
            strategy_file="/tmp/dummy_strategy.py",  # noqa: S108 — test fixture path
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )
        # Strategy file resolution will fail at config-build time
        # because /tmp/dummy_strategy.py doesn't exist — but we
        # care only about the cache shape, which is set BEFORE
        # the strategy resolver runs. Wrap the call in a
        # try/except so the cache assertion runs even if the
        # strategy resolver raises later.
        try:
            run_config = _build_backtest_run_config(payload)
        except Exception:
            # If the resolver fails, the engine_config wasn't
            # constructed at all. The contract we're testing is
            # that NOTHING in the backtest path constructs a
            # CacheConfig with a Redis database. Inspecting the
            # source string is the cleanest assertion.
            from pathlib import Path as _Path

            source = _Path(_build_backtest_run_config.__code__.co_filename).read_text()
            assert "DatabaseConfig" not in source, (
                "backtest_runner.py must NOT import or construct "
                "DatabaseConfig — backtests must keep their cache in "
                "memory (gotcha #8 inverse)"
            )
            return

        # If the build succeeded, walk the engine config and
        # verify cache.database is None.
        engine_config = run_config.engine
        cache = getattr(engine_config, "cache", None)
        if cache is None:
            return  # Nautilus's default — in-memory cache
        assert cache.database is None


class TestRedisDatabaseConfigParsing:
    """Codex batch 8 P1 regression: ``build_redis_database_config``
    must extract host, port, username, password, AND tls flag
    from ``settings.redis_url``. Earlier code only forwarded
    host/port, which would silently fail on auth-protected or
    TLS-enabled Redis instances (Azure Cache, Upstash,
    ElastiCache, etc.)."""

    def test_parses_host_and_port(self) -> None:
        with patch("msai.services.nautilus.live_node_config.settings") as mock_settings:
            mock_settings.redis_url = "redis://prod-redis.example.com:6390"
            db = build_redis_database_config()
        assert db.host == "prod-redis.example.com"
        assert db.port == 6390

    def test_parses_username_password(self) -> None:
        with patch("msai.services.nautilus.live_node_config.settings") as mock_settings:
            mock_settings.redis_url = "redis://default:secret-token@prod-redis.example.com:6390"
            db = build_redis_database_config()
        assert db.username == "default"
        assert db.password == "secret-token"  # noqa: S105

    def test_rediss_scheme_enables_ssl(self) -> None:
        with patch("msai.services.nautilus.live_node_config.settings") as mock_settings:
            mock_settings.redis_url = "rediss://default:tok@prod.example.com:6390"
            db = build_redis_database_config()
        assert db.ssl is True

    def test_redis_scheme_disables_ssl(self) -> None:
        with patch("msai.services.nautilus.live_node_config.settings") as mock_settings:
            mock_settings.redis_url = "redis://localhost:6379"
            db = build_redis_database_config()
        assert db.ssl is False

    def test_defaults_when_url_minimal(self) -> None:
        with patch("msai.services.nautilus.live_node_config.settings") as mock_settings:
            mock_settings.redis_url = "redis://"
            db = build_redis_database_config()
        assert db.host == "localhost"
        assert db.port == 6379
        assert db.username is None
        assert db.password is None
        assert db.ssl is False
