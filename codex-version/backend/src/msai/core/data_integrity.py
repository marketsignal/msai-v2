import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def atomic_write_parquet(table: Any, target_path: Path) -> str:
    """Write a parquet file atomically and return the SHA256 checksum."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".parquet.tmp")
    try:
        os.close(fd)
        pq.write_table(table, tmp_path, compression="zstd")
        checksum = hashlib.sha256(Path(tmp_path).read_bytes()).hexdigest()
        os.rename(tmp_path, target_path)
        return checksum
    except BaseException:
        tmp = Path(tmp_path)
        if tmp.exists():
            tmp.unlink()
        raise


def dedup_bars(df: Any, key_columns: tuple[str, str] = ("symbol", "timestamp")) -> Any:
    """Remove duplicate bars by natural key, keeping the latest row."""
    available = [column for column in key_columns if column in df.columns]
    if not available:
        raise KeyError(f"None of the dedup columns are present: {key_columns}")
    return df.drop_duplicates(subset=available, keep="last")
