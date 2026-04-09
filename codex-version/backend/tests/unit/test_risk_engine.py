import pytest

from msai.services.risk_engine import RiskEngine, RiskMetrics, RiskState


class InMemoryRiskStateStore:
    def __init__(self) -> None:
        self.state = RiskState(halted=False)

    async def get_state(self) -> RiskState:
        return self.state

    def get_state_sync(self) -> RiskState:
        return self.state

    async def set_halt(self, reason: str) -> RiskState:
        self.state = RiskState(halted=True, reason=reason, updated_at="2026-04-06T00:00:00+00:00")
        return self.state

    async def clear_halt(self) -> RiskState:
        self.state = RiskState(halted=False, updated_at="2026-04-06T00:05:00+00:00")
        return self.state


def test_risk_engine_position_limit() -> None:
    engine = RiskEngine(max_position_size=10)
    allowed = engine.check_position_limit("ema", "AAPL", 5)
    blocked = engine.check_position_limit("ema", "AAPL", 11)
    assert allowed.allowed is True
    assert blocked.allowed is False


def test_risk_engine_daily_loss() -> None:
    engine = RiskEngine(max_daily_loss=-100)
    assert engine.check_daily_loss(-50).allowed is True
    assert engine.check_daily_loss(-101).allowed is False


def test_risk_engine_margin_limit() -> None:
    engine = RiskEngine(max_margin_ratio=0.4)
    assert engine.check_margin_usage(1_000.0, 200.0).allowed is True
    assert engine.check_margin_usage(1_000.0, 500.0).allowed is False


@pytest.mark.asyncio
async def test_risk_engine_blocks_new_starts_when_halted() -> None:
    engine = RiskEngine(state_store=InMemoryRiskStateStore())
    await engine.kill_all("manual halt")

    decision = await engine.validate_start(
        strategy="ema",
        instrument="AAPL.XNAS",
        quantity=1.0,
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=1_000_000.0,
            notional_exposure=10_000.0,
            margin_used=10_000.0,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "manual halt"


@pytest.mark.asyncio
async def test_risk_engine_reset_halt_allows_new_starts() -> None:
    store = InMemoryRiskStateStore()
    engine = RiskEngine(state_store=store)
    await engine.kill_all("manual halt")
    await engine.reset_halt()

    decision = await engine.validate_start(
        strategy="ema",
        instrument="AAPL.XNAS",
        quantity=1.0,
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=1_000_000.0,
            notional_exposure=10_000.0,
            margin_used=10_000.0,
        ),
    )

    assert decision.allowed is True


def test_risk_engine_sync_validation_blocks_when_halted() -> None:
    store = InMemoryRiskStateStore()
    store.state = RiskState(halted=True, reason="node halt")
    engine = RiskEngine(state_store=store)

    decision = engine.validate_start_sync(
        strategy="ema",
        instrument="AAPL.XNAS",
        quantity=1.0,
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=1_000_000.0,
            notional_exposure=10_000.0,
            margin_used=10_000.0,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == "node halt"


def test_risk_engine_allows_zero_equity_paper_start_when_flat() -> None:
    engine = RiskEngine(state_store=InMemoryRiskStateStore())

    decision = engine.validate_start_sync(
        strategy="ema",
        instrument="AAPL.XNAS",
        quantity=1.0,
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=0.0,
            notional_exposure=0.0,
            margin_used=0.0,
        ),
        paper_trading=True,
    )

    assert decision.allowed is True


def test_risk_engine_still_blocks_zero_equity_live_start() -> None:
    engine = RiskEngine(state_store=InMemoryRiskStateStore())

    decision = engine.validate_start_sync(
        strategy="ema",
        instrument="AAPL.XNAS",
        quantity=1.0,
        metrics=RiskMetrics(
            current_pnl=0.0,
            portfolio_value=0.0,
            notional_exposure=0.0,
            margin_used=0.0,
        ),
        paper_trading=False,
    )

    assert decision.allowed is False
    assert decision.reason == "invalid portfolio value"
