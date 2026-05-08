"""One-time backfill of parquet_partition_index from the on-disk catalog.

Walks ``{DATA_ROOT}/parquet/<asset_class>/<symbol>/<YYYY>/<MM>.parquet``
and upserts a ``parquet_partition_index`` row for every file. Idempotent —
re-running on an already-populated table simply re-affirms each row.

Usage:
    cd backend && uv run python scripts/build_partition_index.py
    cd backend && uv run python scripts/build_partition_index.py --asset-class stocks --symbol AAPL
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
)
from msai.services.symbol_onboarding.partition_index_db import (
    PartitionIndexGateway,
)

log = get_logger(__name__)


async def _run(asset_class_filter: str | None, symbol_filter: str | None) -> int:
    parquet_root = Path(settings.data_root) / "parquet"
    if not parquet_root.is_dir():
        log.warning("parquet_root_missing", path=str(parquet_root))
        return 0

    indexed = 0
    async with async_session_factory() as session:
        gw = PartitionIndexGateway(session=session)
        svc = PartitionIndexService(db_gateway=gw)

        for ac_dir in sorted(parquet_root.iterdir()):
            if not ac_dir.is_dir():
                continue
            if asset_class_filter and ac_dir.name != asset_class_filter:
                continue

            for sym_dir in sorted(ac_dir.iterdir()):
                if not sym_dir.is_dir():
                    continue
                if symbol_filter and sym_dir.name != symbol_filter:
                    continue

                for year_dir in sorted(sym_dir.iterdir()):
                    if not year_dir.is_dir() or not year_dir.name.isdigit():
                        continue
                    year = int(year_dir.name)

                    for parquet_path in sorted(year_dir.glob("*.parquet")):
                        stem = parquet_path.stem
                        if not stem.isdigit():
                            continue
                        month = int(stem)
                        if not (1 <= month <= 12):
                            continue

                        row = await svc.refresh_for_partition(
                            asset_class=ac_dir.name,
                            symbol=sym_dir.name,
                            year=year,
                            month=month,
                            path=parquet_path,
                        )
                        if row is not None:
                            indexed += 1
                            log.info(
                                "indexed_partition",
                                asset_class=ac_dir.name,
                                symbol=sym_dir.name,
                                year=year,
                                month=month,
                                row_count=row.row_count,
                            )

    log.info("backfill_complete", indexed=indexed)
    return indexed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-class", default=None)
    p.add_argument("--symbol", default=None)
    args = p.parse_args()

    indexed = asyncio.run(_run(args.asset_class, args.symbol))
    print(f"Indexed {indexed} partitions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
