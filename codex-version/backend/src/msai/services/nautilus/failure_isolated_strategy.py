from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from msai.services.nautilus.cache_namespace import install_namespaced_cache

if TYPE_CHECKING:
    from collections.abc import Callable

_RUNTIME_SAFE_MARKER = "_msai_runtime_safe"


class StrategyDegradedError(Exception):
    """Raised when attempting to interact with a degraded strategy."""


class FailureIsolatedStrategy:
    """Mixin that degrades a strategy instead of crashing the whole node."""

    _is_degraded: bool = False
    _WRAPPED_HOOKS: tuple[str, ...] = ("on_start", "on_bar", "on_quote_tick", "on_order_event")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for hook_name in FailureIsolatedStrategy._WRAPPED_HOOKS:
            original = cls.__dict__.get(hook_name)
            if callable(original) and not getattr(original, "_fi_wrapped", False):
                setattr(cls, hook_name, FailureIsolatedStrategy._make_safe_wrapper(hook_name, original))

    @staticmethod
    def _make_safe_wrapper(
        hook_name: str,
        original: Callable[..., Any],
    ) -> Callable[..., Any]:
        def safe_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            FailureIsolatedStrategy._ensure_namespaced_cache(self)
            if getattr(self, "_is_degraded", False):
                log = getattr(self, "log", None)
                if log is not None:
                    log.warning(f"Degraded - skipping {hook_name}")
                return None
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:
                setattr(self, "_is_degraded", True)
                log = getattr(self, "log", None)
                if log is not None:
                    log.error(
                        f"{hook_name} raised {type(exc).__name__}: {exc} - strategy degraded"
                    )
                return None

        safe_wrapper._fi_wrapped = True  # type: ignore[attr-defined]
        return safe_wrapper

    @staticmethod
    def _ensure_namespaced_cache(strategy: Any) -> None:
        if getattr(strategy, "_msai_cache_namespaced", False):
            return
        proxy = install_namespaced_cache(strategy)
        if proxy is None:
            return
        setattr(strategy, "_msai_cache_namespaced", True)


def activate_runtime_strategy_safety(strategy_path: str) -> type[Any]:
    module_name, separator, class_name = strategy_path.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"Invalid strategy path: {strategy_path}")

    module = importlib.import_module(module_name)
    strategy_cls = getattr(module, class_name, None)
    if not isinstance(strategy_cls, type):
        raise TypeError(f"Strategy class not found for path {strategy_path}")
    if getattr(strategy_cls, _RUNTIME_SAFE_MARKER, False):
        return strategy_cls
    if issubclass(strategy_cls, FailureIsolatedStrategy):
        setattr(strategy_cls, _RUNTIME_SAFE_MARKER, True)
        return strategy_cls

    attrs: dict[str, Any] = {
        "__module__": module.__name__,
        "__doc__": strategy_cls.__doc__,
    }
    for hook_name in FailureIsolatedStrategy._WRAPPED_HOOKS:
        original = strategy_cls.__dict__.get(hook_name)
        if callable(original):
            attrs[hook_name] = original

    wrapped = type(class_name, (FailureIsolatedStrategy, strategy_cls), attrs)
    setattr(wrapped, _RUNTIME_SAFE_MARKER, True)
    setattr(module, class_name, wrapped)
    return wrapped
