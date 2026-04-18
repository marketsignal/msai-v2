from __future__ import annotations

from collections.abc import Callable
from typing import Any

_KEYED_METHOD_NAMES = frozenset({"add", "get", "has", "delete", "remove", "pop", "set", "update"})


def namespaced_cache_key(strategy_id_full: str, key: str) -> str:
    if not strategy_id_full:
        raise ValueError("strategy_id_full must not be empty")
    if not key:
        raise ValueError("key must not be empty")
    return f"{strategy_id_full}:{key}"


class NamespacedCacheProxy:
    def __init__(self, cache: Any, strategy_id_full: str) -> None:
        self._cache = cache
        self.strategy_id_full = strategy_id_full

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._cache, name)
        if not callable(attr) or name not in _KEYED_METHOD_NAMES:
            return attr
        return self._wrap_keyed_method(attr)

    def _wrap_keyed_method(self, method: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(key: str, *args: Any, **kwargs: Any) -> Any:
            return method(namespaced_cache_key(self.strategy_id_full, key), *args, **kwargs)

        return wrapped


def install_namespaced_cache(strategy: Any) -> NamespacedCacheProxy | None:
    cache = getattr(strategy, "cache", None)
    strategy_id = getattr(getattr(strategy, "id", None), "value", None)
    if cache is None or not strategy_id:
        return None
    if isinstance(cache, NamespacedCacheProxy) and cache.strategy_id_full == str(strategy_id):
        return cache

    proxy = NamespacedCacheProxy(cache, str(strategy_id))
    assigned = False
    try:
        setattr(strategy, "cache", proxy)
        assigned = True
    except Exception:
        assigned = False
    try:
        setattr(strategy, "cache_ns", proxy)
    except Exception:
        if not assigned:
            return None
    return proxy
