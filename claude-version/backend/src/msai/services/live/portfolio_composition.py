"""Composition hash for LivePortfolioRevision.

The hash is the warm-restart identity boundary — two revisions with the
same hash represent the SAME composition, meaning the supervisor can
warm-restart into either without state loss.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decimal import Decimal


def compute_composition_hash(members: list[dict[str, Any]]) -> str:
    """64-char sha256 hex over the canonical JSON of the sorted member list.

    Each member must contain: ``strategy_id`` (UUID), ``order_index``
    (int), ``config`` (JSON-serializable dict), ``instruments``
    (list[str]), ``weight`` (Decimal).

    Canonicalization rules:
    - sort by ``order_index`` so caller order is irrelevant
    - ``strategy_id`` → 32-char UUID hex
    - ``instruments`` → sorted, de-duped
    - ``weight`` → normalized via ``Decimal.normalize()``, then ``format(..., "f")``
      so ``0.5`` and ``0.50`` hash identically
    - ``config`` → ``sort_keys=True`` at every level
    """
    canonical = [
        _canonicalize_member(m) for m in sorted(members, key=lambda m: m["order_index"])
    ]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonicalize_member(member: dict[str, Any]) -> dict[str, Any]:
    weight: Decimal = member["weight"]
    return {
        "strategy_id": member["strategy_id"].hex,
        "order_index": int(member["order_index"]),
        "config": member["config"],
        "instruments": sorted(set(member["instruments"])),
        "weight": format(weight.normalize(), "f"),
    }
