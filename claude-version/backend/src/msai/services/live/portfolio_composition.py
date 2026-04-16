"""Composition hash for LivePortfolioRevision.

The hash is the warm-restart identity boundary — two revisions with the
same hash represent the SAME composition, meaning the supervisor can
warm-restart into either without state loss.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

# Scale of the ``weight`` column on ``live_portfolio_revision_strategies``
# (``Numeric(8, 6)``). Hashing must quantize to this scale so that a
# caller-supplied ``Decimal("0.3333333")`` hashes identically to the
# rounded value Postgres will persist (``0.333333``). Without this, a
# revision snapshotted pre-flush and one re-read from the DB compute
# different hashes for the same row (Codex review, 2026-04-16).
_WEIGHT_SCALE = Decimal("0.000001")


def compute_composition_hash(members: list[dict[str, Any]]) -> str:
    """64-char sha256 hex over the canonical JSON of the sorted member list.

    Each member must contain: ``strategy_id`` (UUID), ``order_index``
    (int), ``config`` (JSON-serializable dict), ``instruments``
    (list[str]), ``weight`` (Decimal).

    Canonicalization rules:
    - sort by ``order_index`` so caller order is irrelevant
    - ``strategy_id`` → 32-char UUID hex
    - ``instruments`` → sorted, de-duped
    - ``weight`` → quantized to the DB column's scale (6 decimal
      places), then ``Decimal.normalize()``, then ``format(..., "f")``
      so ``0.5``, ``0.50``, and ``0.500000`` all hash identically AND
      match the value Postgres will persist under ``Numeric(8, 6)``
    - ``config`` → ``sort_keys=True`` at every level
    """
    canonical = [_canonicalize_member(m) for m in sorted(members, key=lambda m: m["order_index"])]
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonicalize_member(member: dict[str, Any]) -> dict[str, Any]:
    weight: Decimal = member["weight"]
    quantized = weight.quantize(_WEIGHT_SCALE)
    return {
        "strategy_id": member["strategy_id"].hex,
        "order_index": int(member["order_index"]),
        "config": member["config"],
        "instruments": sorted(set(member["instruments"])),
        "weight": format(quantized.normalize(), "f"),
    }
