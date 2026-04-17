"""Unit tests for LiveDeploymentStrategy."""

from __future__ import annotations

from uuid import uuid4


def test_live_deployment_strategy_imports() -> None:
    from msai.models import LiveDeploymentStrategy

    lds = LiveDeploymentStrategy(
        id=uuid4(),
        deployment_id=uuid4(),
        revision_strategy_id=uuid4(),
        strategy_id_full="EMACross-abcd1234abcd1234",
    )
    assert lds.strategy_id_full == "EMACross-abcd1234abcd1234"


def test_lds_required_columns() -> None:
    from msai.models import LiveDeploymentStrategy

    cols = {c.name: c for c in LiveDeploymentStrategy.__table__.columns}
    for name in ("deployment_id", "revision_strategy_id", "strategy_id_full"):
        assert cols[name].nullable is False
    # Immutable — created_at only.
    assert "updated_at" not in cols
