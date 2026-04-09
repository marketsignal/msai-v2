from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from redis import Redis

from msai.core.config import settings
from msai.core.queue import get_redis_pool


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(slots=True)
class RiskMetrics:
    current_pnl: float
    portfolio_value: float
    notional_exposure: float
    margin_used: float = 0.0


@dataclass(slots=True)
class RiskState:
    halted: bool
    reason: str | None = None
    updated_at: str | None = None


class RiskStateStore:
    HALT_KEY = "risk:halt_state"

    @staticmethod
    def _deserialize_state(raw: str | bytes | None) -> RiskState:
        if raw is None:
            return RiskState(halted=False)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        return RiskState(
            halted=bool(payload.get("halted")),
            reason=str(payload["reason"]) if payload.get("reason") else None,
            updated_at=str(payload["updated_at"]) if payload.get("updated_at") else None,
        )

    async def get_state(self) -> RiskState:
        pool = await get_redis_pool()
        raw = await pool.get(self.HALT_KEY)
        return self._deserialize_state(raw)

    def get_state_sync(self) -> RiskState:
        client = Redis.from_url(settings.redis_url)
        try:
            raw = client.get(self.HALT_KEY)
        finally:
            client.close()
        return self._deserialize_state(raw)

    async def set_halt(self, reason: str) -> RiskState:
        state = RiskState(
            halted=True,
            reason=reason,
            updated_at=datetime.now(UTC).isoformat(),
        )
        pool = await get_redis_pool()
        await pool.set(
            self.HALT_KEY,
            json.dumps(
                {
                    "halted": state.halted,
                    "reason": state.reason,
                    "updated_at": state.updated_at,
                }
            ),
        )
        return state

    async def clear_halt(self) -> RiskState:
        pool = await get_redis_pool()
        await pool.delete(self.HALT_KEY)
        return RiskState(halted=False, updated_at=datetime.now(UTC).isoformat())


class RiskEngine:
    def __init__(
        self,
        max_position_size: float = 500.0,
        max_daily_loss: float = -10_000.0,
        max_exposure: float = 1.0,
        max_margin_ratio: float = 0.5,
        state_store: RiskStateStore | None = None,
    ) -> None:
        self.max_position_size = max_position_size
        self.max_daily_loss = max_daily_loss
        self.max_exposure = max_exposure
        self.max_margin_ratio = max_margin_ratio
        self._state_store = state_store or RiskStateStore()

    def check_position_limit(self, strategy: str, instrument: str, quantity: float) -> RiskDecision:
        _ = strategy, instrument
        if abs(quantity) > self.max_position_size:
            return RiskDecision(False, "position limit exceeded")
        return RiskDecision(True, "ok")

    def check_daily_loss(self, current_pnl: float, threshold: float | None = None) -> RiskDecision:
        limit = threshold if threshold is not None else self.max_daily_loss
        if current_pnl < limit:
            return RiskDecision(False, "daily loss threshold breached")
        return RiskDecision(True, "ok")

    def check_notional_exposure(self, portfolio_value: float, notional_exposure: float) -> RiskDecision:
        if portfolio_value <= 0:
            return RiskDecision(False, "invalid portfolio value")
        ratio = notional_exposure / portfolio_value
        if ratio > self.max_exposure:
            return RiskDecision(False, "notional exposure limit exceeded")
        return RiskDecision(True, "ok")

    def check_margin_usage(self, portfolio_value: float, margin_used: float) -> RiskDecision:
        if portfolio_value <= 0:
            return RiskDecision(False, "invalid portfolio value")
        ratio = margin_used / portfolio_value
        if ratio > self.max_margin_ratio:
            return RiskDecision(False, "margin usage limit exceeded")
        return RiskDecision(True, "ok")

    @staticmethod
    def _paper_start_has_safe_zero_equity(metrics: RiskMetrics, *, paper_trading: bool) -> bool:
        return (
            paper_trading
            and metrics.portfolio_value <= 0
            and metrics.notional_exposure <= 0
            and metrics.margin_used <= 0
        )

    def validate_limits(
        self,
        strategy: str,
        instrument: str,
        quantity: float,
        metrics: RiskMetrics,
        *,
        paper_trading: bool = False,
    ) -> RiskDecision:
        decisions = [
            self.check_position_limit(strategy, instrument, quantity),
            self.check_daily_loss(metrics.current_pnl),
        ]
        if not self._paper_start_has_safe_zero_equity(metrics, paper_trading=paper_trading):
            decisions.extend(
                [
                    self.check_notional_exposure(metrics.portfolio_value, metrics.notional_exposure),
                    self.check_margin_usage(metrics.portfolio_value, metrics.margin_used),
                ]
            )
        for decision in decisions:
            if not decision.allowed:
                return decision
        return RiskDecision(True, "ok")

    async def validate_start(
        self,
        strategy: str,
        instrument: str,
        quantity: float,
        metrics: RiskMetrics,
        *,
        paper_trading: bool = False,
    ) -> RiskDecision:
        try:
            state = await self._state_store.get_state()
        except Exception:
            return RiskDecision(False, "risk state unavailable")

        if state.halted:
            return RiskDecision(False, state.reason or "global halt is active")
        return self.validate_limits(
            strategy,
            instrument,
            quantity,
            metrics,
            paper_trading=paper_trading,
        )

    def validate_start_sync(
        self,
        strategy: str,
        instrument: str,
        quantity: float,
        metrics: RiskMetrics,
        *,
        paper_trading: bool = False,
    ) -> RiskDecision:
        try:
            state = self._state_store.get_state_sync()
        except Exception:
            return RiskDecision(False, "risk state unavailable")

        if state.halted:
            return RiskDecision(False, state.reason or "global halt is active")
        return self.validate_limits(
            strategy,
            instrument,
            quantity,
            metrics,
            paper_trading=paper_trading,
        )

    async def kill_all(self, reason: str = "kill-all triggered") -> RiskDecision:
        try:
            await self._state_store.set_halt(reason)
        except Exception:
            return RiskDecision(False, "failed to persist kill switch state")
        return RiskDecision(True, reason)

    async def reset_halt(self) -> RiskDecision:
        try:
            await self._state_store.clear_halt()
        except Exception:
            return RiskDecision(False, "failed to clear kill switch state")
        return RiskDecision(True, "halt state cleared")

    async def current_state(self) -> RiskState:
        try:
            return await self._state_store.get_state()
        except Exception:
            return RiskState(
                halted=True,
                reason="risk state unavailable",
                updated_at=datetime.now(UTC).isoformat(),
            )
