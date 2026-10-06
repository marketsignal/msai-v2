from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from nautilus_trader.model import BarType, InstrumentId
from nautilus_trader.trading import StrategyConfig
from pydantic import Field


class EMACrossConfig(StrategyConfig):
    def __init__(
        self,
        instrument_id: InstrumentId,
        bar_type: BarType,
        fast_ema_period: Annotated[int, Field(gt=0, strict=True)] = 10,
        slow_ema_period: Annotated[int, Field(gt=0, strict=True)] = 30,
        trade_size: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] = Decimal("1"),
        order_id_tag: str | None = None,
    ) -> None:
        super().__init__()
        self.instrument_id = (
            InstrumentId.from_str(instrument_id)
            if isinstance(instrument_id, str)
            else instrument_id
        )
        self.bar_type = (
            BarType.from_str(bar_type) if isinstance(bar_type, str) else bar_type
        )
        self.fast_ema_period = fast_ema_period
        self.slow_ema_period = slow_ema_period
        self.trade_size = trade_size
