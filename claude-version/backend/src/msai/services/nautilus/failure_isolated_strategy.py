"""FailureIsolatedStrategy mixin for multi-strategy TradingNodes.

Nautilus re-raises exceptions from event handlers (actor.pyx:4468-4472),
which crashes the entire node.  This mixin wraps ``on_bar``,
``on_quote_tick``, and ``on_order_event`` via ``__init_subclass__`` so
a single buggy strategy degrades gracefully instead of taking down all
co-located strategies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class StrategyDegradedError(Exception):
    """Raised when attempting to interact with a degraded strategy."""


class FailureIsolatedStrategy:
    """Mixin that catches and contains exceptions from Nautilus
    event handlers.  Use via multiple inheritance::

        class MyStrategy(FailureIsolatedStrategy, Strategy):
            def on_bar(self, bar): ...

    ``__init_subclass__`` wraps each handler at class-definition
    time so the Cython dispatch sees the safe wrapper, not the
    user's potentially-raising method.
    """

    _is_degraded: bool = False
    _WRAPPED_HOOKS: tuple[str, ...] = ("on_bar", "on_quote_tick", "on_order_event")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for hook_name in FailureIsolatedStrategy._WRAPPED_HOOKS:
            original = cls.__dict__.get(hook_name)
            if original is not None and not getattr(original, "_fi_wrapped", False):
                wrapped = FailureIsolatedStrategy._make_safe_wrapper(hook_name, original)
                setattr(cls, hook_name, wrapped)

    @staticmethod
    def _make_safe_wrapper(
        hook_name: str,
        original: Callable[..., Any],
    ) -> Callable[..., Any]:
        def safe_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if self._is_degraded:
                self.log.warning(f"Degraded — skipping {hook_name}")
                return None
            try:
                return original(self, *args, **kwargs)
            except Exception as exc:
                self._is_degraded = True
                self.log.error(
                    f"{hook_name} raised {type(exc).__name__}: {exc} — strategy degraded"
                )
                return None

        safe_wrapper._fi_wrapped = True  # type: ignore[attr-defined]
        return safe_wrapper
