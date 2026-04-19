"""Unit tests for the cache-key namespace helper.

Nautilus cache is global — no per-strategy namespacing. This helper
prefixes cache keys with the strategy_id_full so different strategies
running in the same TradingNode don't collide.
"""

from __future__ import annotations

import pytest

from msai.services.nautilus.cache_namespace import namespaced_cache_key


class TestNamespacedCacheKey:
    def test_prefixes_with_strategy_id(self) -> None:
        key = namespaced_cache_key("EMACross-abc123", "ema_fast")
        assert key == "EMACross-abc123:ema_fast"

    def test_empty_strategy_id_raises(self) -> None:
        with pytest.raises(ValueError, match="strategy_id_full"):
            namespaced_cache_key("", "ema_fast")

    def test_preserves_colons_in_base_key(self) -> None:
        key = namespaced_cache_key("EMACross-abc123", "ema:fast:period")
        assert key == "EMACross-abc123:ema:fast:period"

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError, match="key"):
            namespaced_cache_key("EMACross-abc123", "")
