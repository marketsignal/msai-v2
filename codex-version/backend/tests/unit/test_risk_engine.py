from msai.services.risk_engine import RiskEngine


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
