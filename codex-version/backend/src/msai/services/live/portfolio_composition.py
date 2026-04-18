from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

_WEIGHT_SCALE = Decimal("0.000001")
_WEIGHT_ROUNDING = ROUND_HALF_EVEN


def compute_composition_hash(members: list[dict[str, Any]]) -> str:
    canonical = [_canonicalize_member(member) for member in sorted(members, key=lambda item: int(item["order_index"]))]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonicalize_member(member: dict[str, Any]) -> dict[str, Any]:
    weight = Decimal(str(member["weight"])).quantize(_WEIGHT_SCALE, rounding=_WEIGHT_ROUNDING)
    return {
        "strategy_id": str(member["strategy_id"]),
        "order_index": int(member["order_index"]),
        "config": dict(member["config"]),
        "instruments": sorted(set(str(value) for value in member["instruments"])),
        "weight": format(weight.normalize(), "f"),
    }
