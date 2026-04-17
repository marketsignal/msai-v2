"""Cache-key namespace helper for NautilusTrader.

Nautilus cache is global -- no per-strategy namespacing. When multiple
strategies run in the same ``TradingNode`` (portfolio-based deployments),
user-defined cache keys (e.g. ``"ema_fast"``) would collide across
strategies.

This module provides a simple prefix helper that namespaces cache keys
under the strategy's ``strategy_id_full`` (e.g. ``"EMACross-0-abc123"``),
producing keys like ``"EMACross-0-abc123:ema_fast"``.

Usage in a strategy's ``on_start()``::

    from msai.services.nautilus.cache_namespace import namespaced_cache_key

    key = namespaced_cache_key(self.id.value, "ema_fast")
    self.cache.add(key, value)
"""

from __future__ import annotations


def namespaced_cache_key(strategy_id_full: str, key: str) -> str:
    """Return ``"{strategy_id_full}:{key}"`` with input validation.

    Parameters
    ----------
    strategy_id_full:
        The Nautilus ``StrategyId.value`` string, e.g. ``"EMACross-0-abc123"``.
    key:
        The base cache key, e.g. ``"ema_fast"``.

    Returns
    -------
    str
        Namespaced key in the form ``"EMACross-0-abc123:ema_fast"``.

    Raises
    ------
    ValueError
        If ``strategy_id_full`` or ``key`` is empty.
    """
    if not strategy_id_full:
        raise ValueError("strategy_id_full must not be empty")
    if not key:
        raise ValueError("key must not be empty")
    return f"{strategy_id_full}:{key}"
