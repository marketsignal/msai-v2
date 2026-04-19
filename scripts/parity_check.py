"""Parity check script (Phase 2 task 2.11).

Operator entry point for the post-hoc backtest-vs-paper-soak
comparison documented in the plan:

1. Phase 5 paper soak runs the strategy live and writes
   ``order_attempt_audits`` rows.
2. The operator re-runs the same strategy + same config in
   backtest against the same Parquet window via this script.
3. The script normalizes both sources into ``OrderIntent``
   sequences and runs the comparator.
4. Empty divergence list → parity OK; otherwise the script
   exits non-zero with a structured diff so CI / on-call can
   page on drift.

Both inputs are CSV files in the Nautilus orders-report shape
(or any DataFrame with the columns the normalizer accepts).
The script intentionally does NOT spawn a backtest itself —
the operator runs the backtest separately and pipes the
``orders_df`` into a CSV the script reads. Keeping the script
focused on comparison makes it composable with both ad-hoc
investigation and the Phase 5 acceptance harness.

Usage::

    python claude-version/scripts/parity_check.py \\
        --left  data/parity/live_orders_2026_03_15.csv \\
        --right data/parity/backtest_orders_2026_03_15.csv

Exit codes:

- ``0``: sequences match
- ``1``: sequences diverge (with detailed report on stdout)
- ``2``: input file missing or unreadable
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add backend/src to sys.path so the script can import the parity
# package without being installed as a package itself.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from msai.services.nautilus.parity.comparator import (  # noqa: E402
    Divergence,
    DivergenceKind,
    compare,
)
from msai.services.nautilus.parity.normalizer import (  # noqa: E402
    normalize_orders_df,
)


def _format_divergence(d: Divergence) -> str:
    """Render a single divergence record into a one-line
    operator-facing string."""
    if d.kind == DivergenceKind.FIELD_MISMATCH:
        return f"  [{d.index}] FIELD_MISMATCH\n      left : {d.left}\n      right: {d.right}"
    if d.kind == DivergenceKind.EXTRA_LEFT:
        return f"  [{d.index}] EXTRA_LEFT  {d.left}"
    if d.kind == DivergenceKind.EXTRA_RIGHT:
        return f"  [{d.index}] EXTRA_RIGHT {d.right}"
    if d.kind == DivergenceKind.LENGTH_MISMATCH:
        return f"  [{d.index}] LENGTH_MISMATCH"
    return f"  [{d.index}] {d.kind}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two order-intent CSVs for parity drift.",
    )
    parser.add_argument(
        "--left",
        type=Path,
        required=True,
        help="Path to the LEFT CSV (typically the live audit log).",
    )
    parser.add_argument(
        "--right",
        type=Path,
        required=True,
        help="Path to the RIGHT CSV (typically the backtest replay).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.left.exists():
        print(f"ERROR: --left path not found: {args.left}", file=sys.stderr)
        return 2
    if not args.right.exists():
        print(f"ERROR: --right path not found: {args.right}", file=sys.stderr)
        return 2

    left_df = pd.read_csv(args.left)
    right_df = pd.read_csv(args.right)

    left_intents = normalize_orders_df(left_df)
    right_intents = normalize_orders_df(right_df)

    print(f"left  intents: {len(left_intents)}")
    print(f"right intents: {len(right_intents)}")

    divergences = compare(left_intents, right_intents)
    if not divergences:
        print("PARITY OK — sequences are identical.")
        return 0

    print(f"PARITY FAILED — {len(divergences)} divergence(s):")
    for d in divergences:
        print(_format_divergence(d))
    return 1


if __name__ == "__main__":
    sys.exit(main())
