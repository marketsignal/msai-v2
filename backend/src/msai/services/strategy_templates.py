"""Strategy template scaffolding service.

Provides pre-built templates (mean_reversion_zscore, ema_cross,
donchian_breakout) that users can instantiate into new strategy files
under ``strategies/``.

Ported from the Codex implementation with key adaptations:
- Generated strategies do NOT define ``on_stop()`` -- Claude handles
  stop/flatten via ``manage_stop=True`` in the live node config.
- Uses Claude version's ``discover_strategies`` for validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any

from msai.core.config import settings
from msai.core.logging import get_logger

log = get_logger(__name__)


class StrategyTemplateError(ValueError):
    """Raised when strategy scaffolding input is invalid."""


@dataclass(frozen=True, slots=True)
class StrategyTemplateDefinition:
    """Immutable definition of a strategy template."""

    id: str
    label: str
    description: str
    default_config: dict[str, Any]


TEMPLATES: tuple[StrategyTemplateDefinition, ...] = (
    StrategyTemplateDefinition(
        id="mean_reversion_zscore",
        label="Mean Reversion Z-Score",
        description=(
            "Intraday bar-based z-score mean reversion with configurable"
            " hold window and optional shorting."
        ),
        default_config={
            "lookback": 20,
            "entry_zscore": 1.5,
            "exit_zscore": 0.25,
            "trade_size": "1",
            "max_hold_bars": 30,
            "allow_shorting": True,
        },
    ),
    StrategyTemplateDefinition(
        id="ema_cross",
        label="EMA Cross",
        description=(
            "Trend-following EMA crossover strategy with fast/slow periods and fixed trade size."
        ),
        default_config={
            "fast_ema_period": 10,
            "slow_ema_period": 30,
            "trade_size": "1",
        },
    ),
    StrategyTemplateDefinition(
        id="donchian_breakout",
        label="Donchian Breakout",
        description=(
            "Simple breakout system with independent entry/exit lookbacks and optional shorting."
        ),
        default_config={
            "entry_lookback": 20,
            "exit_lookback": 10,
            "trade_size": "1",
            "allow_shorting": True,
        },
    ),
)


class StrategyTemplateService:
    """Scaffold new NautilusTrader strategies from pre-built templates."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.strategies_root

    def list_templates(self) -> list[dict[str, Any]]:
        """Return metadata for every available template."""
        return [
            {
                "id": t.id,
                "label": t.label,
                "description": t.description,
                "default_config": t.default_config,
            }
            for t in TEMPLATES
        ]

    def scaffold(
        self,
        *,
        template_id: str,
        module_name: str,
        description: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Generate a strategy file from a template.

        Args:
            template_id: One of the registered template IDs.
            module_name: Dotted Python module name (e.g. ``"user.my_strat"``).
                Maps to ``strategies/user/my_strat.py``.
            description: Optional docstring override for the strategy class.
            force: Overwrite an existing file if ``True``.

        Returns:
            Dict with ``template_id``, ``name``, ``file_path``,
            ``strategy_class``.

        Raises:
            StrategyTemplateError: On invalid input or path-traversal attempt.
        """
        template = next((t for t in TEMPLATES if t.id == template_id), None)
        if template is None:
            raise StrategyTemplateError(f"Unknown strategy template: {template_id}")

        segments = _validate_module_name(module_name)
        relative_path = Path(*segments).with_suffix(".py")
        file_path = (self.root / relative_path).resolve()
        root_path = self.root.resolve()

        # Guard against path traversal (e.g. module_name = "../../../etc/passwd")
        if root_path not in file_path.parents:
            raise StrategyTemplateError("Strategy module path escapes the strategies root")

        if file_path.exists() and not force:
            raise StrategyTemplateError(f"Strategy file already exists: {relative_path.as_posix()}")

        class_prefix = _pascal_case(segments[-1])
        config_class = f"{class_prefix}Config"
        strategy_class = f"{class_prefix}Strategy"
        module_doc = (description or template.description).strip()

        # Render the description as a properly-escaped Python string
        # literal via ``repr()`` rather than interpolating it raw inside
        # a triple-quoted docstring. ``repr()`` handles every edge case:
        # description ending in ``"`` (which would close the docstring
        # prematurely), embedded ``"""`` (injection vector), embedded
        # newlines, backslashes, control chars. Without this, inputs
        # like ``"Fast EMA"`` produce a syntactically invalid file that
        # the next sync fails to import (Codex iter-2 P2 2026-05-15,
        # after the read-only :ro mount was removed).
        module_doc_literal = _safe_docstring_literal(module_doc)

        source = _render_template(
            template_id=template.id,
            strategy_class=strategy_class,
            config_class=config_class,
            module_doc_literal=module_doc_literal,
        )

        # Create directories and __init__.py files
        self.root.mkdir(parents=True, exist_ok=True)
        _ensure_package_dirs(self.root, relative_path.parent)
        file_path.write_text(source)

        log.info(
            "strategy_scaffolded",
            template_id=template_id,
            module_name=module_name,
            file_path=str(relative_path),
        )

        return {
            "template_id": template.id,
            "name": module_name,
            "file_path": relative_path.as_posix(),
            "strategy_class": strategy_class,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_module_name(module_name: str) -> list[str]:
    """Parse and validate a dotted module name into path segments."""
    text = module_name.strip().replace("/", ".")
    if not text:
        raise StrategyTemplateError("Module name is required")
    segments = text.split(".")
    for segment in segments:
        if not segment:
            raise StrategyTemplateError("Module names cannot contain empty path segments")
        if not segment.replace("_", "").isalnum() or not (
            segment[0].isalpha() or segment[0] == "_"
        ):
            raise StrategyTemplateError(
                "Module names must be valid Python identifiers separated by dots"
            )
    return segments


def _pascal_case(value: str) -> str:
    """Convert a snake_case identifier to PascalCase."""
    return "".join(part[:1].upper() + part[1:] for part in value.split("_") if part)


def _ensure_package_dirs(root: Path, relative_dir: Path) -> None:
    """Create intermediate directories with ``__init__.py`` files."""
    current = root
    for part in relative_dir.parts:
        current = current / part
        current.mkdir(parents=True, exist_ok=True)
        init_file = current / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")


def _safe_docstring_literal(text: str) -> str:
    """Return ``text`` as a valid Python string literal safe to embed as a docstring.

    Uses :func:`repr` so every quoting edge case (terminal double-quote,
    embedded triple-double-quotes, embedded newlines/backslashes/control
    chars) is handled by the language's own escape rules. The returned
    literal is single-line — multi-line descriptions become a single
    string with escaped newlines, which is still a valid class docstring.
    """
    return repr(text)


def _render_template(
    *,
    template_id: str,
    strategy_class: str,
    config_class: str,
    module_doc_literal: str,
) -> str:
    """Dispatch to the appropriate template renderer.

    ``module_doc_literal`` is a pre-escaped Python string literal (see
    :func:`_safe_docstring_literal`), embedded verbatim as the class
    body's first statement.
    """
    if template_id == "mean_reversion_zscore":
        return _render_mean_reversion(strategy_class, config_class, module_doc_literal)
    if template_id == "ema_cross":
        return _render_ema_cross(strategy_class, config_class, module_doc_literal)
    if template_id == "donchian_breakout":
        return _render_donchian(strategy_class, config_class, module_doc_literal)
    raise StrategyTemplateError(f"Unsupported strategy template: {template_id}")


# ---------------------------------------------------------------------------
# Template renderers
#
# NOTE: Generated strategies do NOT define on_stop().  Claude version
# handles stop/flatten via manage_stop=True in live_node_config.
# ---------------------------------------------------------------------------


def _render_mean_reversion(strategy_class: str, config_class: str, module_doc_literal: str) -> str:
    return dedent(
        f"""\
        from __future__ import annotations

        from collections import deque
        from decimal import Decimal
        from statistics import fmean, pstdev

        from nautilus_trader.model.data import Bar, BarType
        from nautilus_trader.model.enums import OrderSide
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.model.objects import Quantity
        from nautilus_trader.trading.config import StrategyConfig
        from nautilus_trader.trading.strategy import Strategy


        class {config_class}(StrategyConfig, frozen=True):
            instrument_id: InstrumentId
            bar_type: BarType
            lookback: int = 20
            entry_zscore: float = 1.5
            exit_zscore: float = 0.25
            trade_size: Decimal = Decimal("1")
            max_hold_bars: int = 30
            allow_shorting: bool = True


        class {strategy_class}(Strategy):
            {module_doc_literal}

            def __init__(self, config: {config_class}) -> None:
                super().__init__(config=config)
                self.instrument_id = config.instrument_id
                self.bar_type = config.bar_type
                self.lookback = max(2, int(config.lookback))
                self.entry_zscore = float(config.entry_zscore)
                self.exit_zscore = max(0.0, float(config.exit_zscore))
                self.trade_size = Quantity.from_str(str(config.trade_size))
                self.max_hold_bars = max(1, int(config.max_hold_bars))
                self.allow_shorting = bool(config.allow_shorting)

                self._closes: deque[float] = deque(maxlen=self.lookback)
                self._bars_in_position = 0

            def on_start(self) -> None:
                self.subscribe_bars(self.bar_type)

            def on_bar(self, bar: Bar) -> None:
                close_price = float(bar.close)
                self._closes.append(close_price)

                if len(self._closes) < self.lookback:
                    return

                stddev = pstdev(self._closes)
                if stddev <= 0:
                    return

                mean_price = fmean(self._closes)
                zscore = (close_price - mean_price) / stddev

                if self.portfolio.is_flat(self.instrument_id):
                    self._bars_in_position = 0
                    if zscore <= -self.entry_zscore:
                        self._submit_market_order(OrderSide.BUY)
                    elif self.allow_shorting and zscore >= self.entry_zscore:
                        self._submit_market_order(OrderSide.SELL)
                    return

                self._bars_in_position += 1
                if self._should_exit(zscore):
                    self.close_all_positions(self.instrument_id)
                    self._bars_in_position = 0

            def _should_exit(self, zscore: float) -> bool:
                if self._bars_in_position >= self.max_hold_bars:
                    return True
                if self.portfolio.is_net_long(self.instrument_id):
                    return zscore >= -self.exit_zscore
                if self.portfolio.is_net_short(self.instrument_id):
                    return zscore <= self.exit_zscore
                return False

            def _submit_market_order(self, side: OrderSide) -> None:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=side,
                    quantity=self.trade_size,
                )
                self.submit_order(order)
        """
    )


def _render_ema_cross(strategy_class: str, config_class: str, module_doc_literal: str) -> str:
    return dedent(
        f"""\
        from __future__ import annotations

        from decimal import Decimal

        from nautilus_trader.indicators import ExponentialMovingAverage
        from nautilus_trader.model.data import Bar, BarType
        from nautilus_trader.model.enums import OrderSide
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.model.objects import Quantity
        from nautilus_trader.trading.config import StrategyConfig
        from nautilus_trader.trading.strategy import Strategy


        class {config_class}(StrategyConfig, frozen=True):
            instrument_id: InstrumentId
            bar_type: BarType
            fast_ema_period: int = 10
            slow_ema_period: int = 30
            trade_size: Decimal = Decimal("1")


        class {strategy_class}(Strategy):
            {module_doc_literal}

            def __init__(self, config: {config_class}) -> None:
                super().__init__(config=config)
                self.instrument_id = config.instrument_id
                self.bar_type = config.bar_type
                self.trade_size = Quantity.from_str(str(config.trade_size))

                self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
                self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

            def on_start(self) -> None:
                self.register_indicator_for_bars(self.bar_type, self.fast_ema)
                self.register_indicator_for_bars(self.bar_type, self.slow_ema)
                self.subscribe_bars(self.bar_type)

            def on_bar(self, bar: Bar) -> None:
                if not self.fast_ema.initialized or not self.slow_ema.initialized:
                    return

                if self.fast_ema.value > self.slow_ema.value:
                    if self.portfolio.is_flat(self.instrument_id):
                        self._submit_market_order(OrderSide.BUY)
                    elif self.portfolio.is_net_short(self.instrument_id):
                        self.close_all_positions(self.instrument_id)
                        self._submit_market_order(OrderSide.BUY)
                elif self.fast_ema.value < self.slow_ema.value:
                    if self.portfolio.is_net_long(self.instrument_id):
                        self.close_all_positions(self.instrument_id)

            def _submit_market_order(self, side: OrderSide) -> None:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=side,
                    quantity=self.trade_size,
                )
                self.submit_order(order)
        """
    )


def _render_donchian(strategy_class: str, config_class: str, module_doc_literal: str) -> str:
    return dedent(
        f"""\
        from __future__ import annotations

        from collections import deque
        from decimal import Decimal

        from nautilus_trader.model.data import Bar, BarType
        from nautilus_trader.model.enums import OrderSide
        from nautilus_trader.model.identifiers import InstrumentId
        from nautilus_trader.model.objects import Quantity
        from nautilus_trader.trading.config import StrategyConfig
        from nautilus_trader.trading.strategy import Strategy


        class {config_class}(StrategyConfig, frozen=True):
            instrument_id: InstrumentId
            bar_type: BarType
            entry_lookback: int = 20
            exit_lookback: int = 10
            trade_size: Decimal = Decimal("1")
            allow_shorting: bool = True


        class {strategy_class}(Strategy):
            {module_doc_literal}

            def __init__(self, config: {config_class}) -> None:
                super().__init__(config=config)
                self.instrument_id = config.instrument_id
                self.bar_type = config.bar_type
                self.entry_lookback = max(2, int(config.entry_lookback))
                self.exit_lookback = max(2, int(config.exit_lookback))
                self.trade_size = Quantity.from_str(str(config.trade_size))
                self.allow_shorting = bool(config.allow_shorting)

                maxlen = max(self.entry_lookback, self.exit_lookback) + 1
                self._highs: deque[float] = deque(maxlen=maxlen)
                self._lows: deque[float] = deque(maxlen=maxlen)
                self._closes: deque[float] = deque(maxlen=maxlen)

            def on_start(self) -> None:
                self.subscribe_bars(self.bar_type)

            def on_bar(self, bar: Bar) -> None:
                self._highs.append(float(bar.high))
                self._lows.append(float(bar.low))
                self._closes.append(float(bar.close))

                if len(self._closes) <= self.entry_lookback:
                    return

                previous_highs = list(self._highs)[:-1]
                previous_lows = list(self._lows)[:-1]
                close_price = self._closes[-1]

                entry_high = max(previous_highs[-self.entry_lookback:])
                entry_low = min(previous_lows[-self.entry_lookback:])
                exit_high = max(previous_highs[-self.exit_lookback:])
                exit_low = min(previous_lows[-self.exit_lookback:])

                if self.portfolio.is_flat(self.instrument_id):
                    if close_price >= entry_high:
                        self._submit_market_order(OrderSide.BUY)
                    elif self.allow_shorting and close_price <= entry_low:
                        self._submit_market_order(OrderSide.SELL)
                    return

                if self.portfolio.is_net_long(self.instrument_id) and close_price <= exit_low:
                    self.close_all_positions(self.instrument_id)
                elif self.portfolio.is_net_short(self.instrument_id) and close_price >= exit_high:
                    self.close_all_positions(self.instrument_id)

            def _submit_market_order(self, side: OrderSide) -> None:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=side,
                    quantity=self.trade_size,
                )
                self.submit_order(order)
        """
    )
