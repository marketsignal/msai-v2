"""Unit tests for portfolio composition hashing.

The composition hash is the warm-restart identity boundary. Any change
to members/configs/instruments/weights/order produces a different hash
→ forces a cold restart.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

_S1 = UUID("11111111-1111-1111-1111-111111111111")
_S2 = UUID("22222222-2222-2222-2222-222222222222")


def _member(
    strategy_id: UUID,
    order_index: int,
    config: dict | None = None,
    instruments: list[str] | None = None,
    weight: Decimal = Decimal("0.5"),
) -> dict:
    return {
        "strategy_id": strategy_id,
        "config": config or {"fast": 10},
        "instruments": instruments or ["AAPL.NASDAQ"],
        "weight": weight,
        "order_index": order_index,
    }


def test_hash_deterministic() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    members = [_member(_S1, 0), _member(_S2, 1)]
    assert compute_composition_hash(members) == compute_composition_hash(members)


def test_hash_stable_across_unordered_input() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0), _member(_S2, 1)]
    b = [_member(_S2, 1), _member(_S1, 0)]
    assert compute_composition_hash(a) == compute_composition_hash(b)


def test_hash_differs_on_weight_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, weight=Decimal("0.5"))]
    b = [_member(_S1, 0, weight=Decimal("0.6"))]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_config_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, config={"fast": 10})]
    b = [_member(_S1, 0, config={"fast": 12})]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_instruments_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, instruments=["AAPL.NASDAQ"])]
    b = [_member(_S1, 0, instruments=["AAPL.NASDAQ", "MSFT.NASDAQ"])]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_strategy_added() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0)]
    b = [_member(_S1, 0), _member(_S2, 1)]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_differs_on_order_change() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0), _member(_S2, 1)]
    b = [_member(_S1, 1), _member(_S2, 0)]
    assert compute_composition_hash(a) != compute_composition_hash(b)


def test_hash_empty_stable() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    assert compute_composition_hash([]) == compute_composition_hash([])


def test_hash_decimal_weights_normalize() -> None:
    from msai.services.live.portfolio_composition import compute_composition_hash

    a = [_member(_S1, 0, weight=Decimal("0.5"))]
    b = [_member(_S1, 0, weight=Decimal("0.50"))]
    assert compute_composition_hash(a) == compute_composition_hash(b)
