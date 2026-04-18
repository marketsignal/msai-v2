from __future__ import annotations

import pytest

from msai.services.nautilus.cache_namespace import NamespacedCacheProxy, namespaced_cache_key


class _CacheStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def add(self, key: str, value: object) -> object:
        self.calls.append((key, value))
        return value

    def raw(self) -> str:
        return "ok"


def test_namespaced_cache_key_prefixes_strategy_id() -> None:
    assert namespaced_cache_key("EMACross-0-abc123", "ema_fast") == "EMACross-0-abc123:ema_fast"


def test_namespaced_cache_key_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError, match="strategy_id_full"):
        namespaced_cache_key("", "ema_fast")
    with pytest.raises(ValueError, match="key"):
        namespaced_cache_key("EMACross-0-abc123", "")


def test_namespaced_cache_proxy_only_prefixes_keyed_methods() -> None:
    cache = _CacheStub()
    proxy = NamespacedCacheProxy(cache, "MeanReversion-1-slug123")

    result = proxy.add("zscore-window", 42)

    assert result == 42
    assert cache.calls == [("MeanReversion-1-slug123:zscore-window", 42)]
    assert proxy.raw() == "ok"
