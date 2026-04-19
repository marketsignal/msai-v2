"""Migrate existing Parquet catalogs to canonical instrument IDs
(Phase 2 task 2.8).

Before Phase 2, MSAI built Nautilus catalogs under the synthetic
``*.SIM`` venue. Phase 2 switches to canonical IB venues
(``AAPL.NASDAQ``, ``ESM5.XCME``, …) so backtest and live trading
see the SAME instrument objects. This script rebuilds existing
catalogs under the new canonical IDs.

Layout:

- INPUT:  ``{data_root}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet``
- OUTPUT: ``{data_root}/nautilus/{canonical_id}/`` (e.g.
  ``{data_root}/nautilus/AAPL.NASDAQ/``)

Behavior:

- **Idempotent**: re-running on an already-migrated catalog is a
  no-op — the streaming catalog builder's own idempotency guard
  (via ``build_catalog_for_symbol``) detects the existing bars
  and skips rebuilding.
- **Shorthand-aware**: walks the per-symbol directories, infers
  the canonical ID via ``InstrumentSpec`` (asset_class +
  default venue) and delegates to the streaming builder.
- **Fail-loud on missing raw data**: if a directory exists but has
  no ``*.parquet`` files, the builder raises ``FileNotFoundError``
  and we log it but continue to the next symbol.

Usage::

    python scripts/migrate_catalog_to_canonical.py \\
        --data-root /app/data \\
        --asset-class stocks

Lives under top-level ``scripts/`` (user-invokable scripts), NOT under
``backend/scripts/``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure we can import from ``backend/src`` when the script is run
# from the worktree root. Matches how ``scripts/e2e_phase1.sh``
# boots its Python context.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from msai.services.nautilus.catalog_builder import (  # noqa: E402
    build_catalog_for_symbol,
)
from msai.services.nautilus.instruments import DEFAULT_EQUITY_VENUE  # noqa: E402

log = logging.getLogger("migrate_catalog")


def discover_symbols(raw_parquet_root: Path, asset_class: str) -> list[str]:
    """List every symbol directory under
    ``{raw_parquet_root}/{asset_class}/``. Returns an alphabetically
    sorted list of the directory names (which are treated as
    ticker symbols)."""
    asset_dir = raw_parquet_root / asset_class
    if not asset_dir.exists():
        return []
    return sorted(p.name for p in asset_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def migrate_one_symbol(
    symbol: str,
    *,
    raw_parquet_root: Path,
    catalog_root: Path,
    asset_class: str,
    venue: str,
) -> str | None:
    """Migrate a single symbol's raw Parquet data into a Nautilus
    catalog keyed by the canonical ID.

    Returns the canonical instrument ID on success or ``None`` on
    a missing-data error (logged but not re-raised, so the batch
    loop keeps going).
    """
    try:
        canonical_id = build_catalog_for_symbol(
            symbol=symbol,
            raw_parquet_root=raw_parquet_root,
            catalog_root=catalog_root,
            asset_class=asset_class,
        )
        log.info("migrated symbol=%s canonical=%s", symbol, canonical_id)
        return canonical_id
    except FileNotFoundError as exc:
        log.warning("skipping %s: %s", symbol, exc)
        return None


def run(
    *,
    data_root: Path,
    asset_class: str = "stocks",
    venue: str = DEFAULT_EQUITY_VENUE,
) -> tuple[int, int]:
    """Top-level migration entry point.

    Args:
        data_root: Root of the MSAI data tree. Raw inputs live at
            ``{data_root}/parquet/``; Nautilus catalog output lives
            at ``{data_root}/nautilus/``.
        asset_class: Subdirectory under ``parquet/`` to migrate
            (default ``stocks``).
        venue: Canonical venue to bind bare symbols to — defaults
            to ``DEFAULT_EQUITY_VENUE`` (``NASDAQ``).

    Returns:
        ``(migrated, skipped)`` — counts of symbols successfully
        rebuilt vs. skipped due to missing raw data.
    """
    raw_root = data_root / "parquet"
    catalog_root = data_root / "nautilus"

    symbols = discover_symbols(raw_root, asset_class)
    if not symbols:
        log.warning("no symbol directories found under %s/%s", raw_root, asset_class)
        return (0, 0)

    migrated = 0
    skipped = 0
    for symbol in symbols:
        result = migrate_one_symbol(
            symbol,
            raw_parquet_root=raw_root,
            catalog_root=catalog_root,
            asset_class=asset_class,
            venue=venue,
        )
        if result is None:
            skipped += 1
        else:
            migrated += 1

    return (migrated, skipped)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Parquet catalogs to canonical IB venues.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Root of the MSAI data tree (contains parquet/ + nautilus/).",
    )
    parser.add_argument(
        "--asset-class",
        default="stocks",
        help="Sub-directory under parquet/ to migrate (default: stocks).",
    )
    parser.add_argument(
        "--venue",
        default=DEFAULT_EQUITY_VENUE,
        help=f"Canonical IB venue (default: {DEFAULT_EQUITY_VENUE}).",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    migrated, skipped = run(
        data_root=args.data_root,
        asset_class=args.asset_class,
        venue=args.venue,
    )
    log.info("catalog migration complete: migrated=%d skipped=%d", migrated, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
