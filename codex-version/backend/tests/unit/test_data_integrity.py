from pathlib import Path

import pandas as pd
import pyarrow as pa

from msai.core.data_integrity import atomic_write_parquet, dedup_bars


def test_dedup_bars_keeps_last() -> None:
    df = pd.DataFrame(
        [
            {"symbol": "AAPL", "timestamp": "2024-01-01T10:00:00", "close": 1},
            {"symbol": "AAPL", "timestamp": "2024-01-01T10:00:00", "close": 2},
        ]
    )
    out = dedup_bars(df)
    assert len(out) == 1
    assert int(out.iloc[0]["close"]) == 2


def test_atomic_write_parquet(tmp_path: Path) -> None:
    table = pa.Table.from_pydict({"a": [1, 2]})
    target = tmp_path / "x.parquet"
    checksum = atomic_write_parquet(table, target)
    assert target.exists()
    assert len(checksum) == 64
