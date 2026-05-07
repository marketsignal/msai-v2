"""Parquet partition-footer reader + DB-cache service.

Two layers:

* :func:`read_parquet_footer` — pure filesystem read. Opens the parquet
  footer (no row read), pulls min/max of the ``timestamp`` column from
  the per-column statistics, returns a :class:`PartitionFooter` plus
  the file's mtime/size for cache invalidation.

* :class:`PartitionIndexService` — DB-cache layer. Reads from
  ``parquet_partition_index``; refreshes lazily on mtime/size mismatch;
  exposes ``get_for_symbol(asset_class, symbol)`` used by day-precise
  ``compute_coverage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

__all__ = [
    "CacheRefreshMisuseError",
    "PartitionFooter",
    "PartitionIndexGatewayProto",
    "PartitionIndexService",
    "PartitionRow",
    "make_refresh_callback",
    "read_parquet_footer",
]


class CacheRefreshMisuseError(RuntimeError):
    """Raised by :func:`make_refresh_callback`'s callback when invoked from
    inside a running event loop.

    Signals a caller-contract violation: ``ParquetStore.write_bars`` must be
    called from a sync context. When invoked from async code, wrap the call
    in ``await asyncio.to_thread(store.write_bars, ...)``. This is distinct
    from runtime cache-update failures (DB down, transient network) so the
    writer's outer ``except`` can let it propagate instead of swallowing it
    as a transient failure.
    """


@dataclass(frozen=True, slots=True)
class PartitionFooter:
    min_ts: datetime
    max_ts: datetime
    row_count: int
    file_mtime: float
    file_size: int


def read_parquet_footer(path: Path) -> PartitionFooter | None:
    """Return footer metadata for a parquet file, or ``None`` if the
    file is missing, unreadable, or lacks a ``timestamp`` column.

    Reads only the parquet footer (no row data) via
    ``ParquetFile.metadata`` + per-column statistics. This stays
    sub-millisecond even for multi-million-row files.
    """
    if not path.is_file():
        return None

    try:
        stat = path.stat()
        pf = pq.ParquetFile(path)
        schema = pf.schema_arrow
        ts_idx = next(
            (i for i, name in enumerate(schema.names) if name == "timestamp"),
            None,
        )
        if ts_idx is None:
            log.warning("parquet_footer_no_timestamp_column", path=str(path))
            return None

        meta = pf.metadata
        if meta.num_rows == 0:
            return None

        # Aggregate min/max across row groups.
        min_ts: datetime | None = None
        max_ts: datetime | None = None
        for rg_idx in range(meta.num_row_groups):
            stats = meta.row_group(rg_idx).column(ts_idx).statistics
            if stats is None or not stats.has_min_max:
                continue
            rg_min = _coerce_datetime(stats.min)
            rg_max = _coerce_datetime(stats.max)
            if min_ts is None or rg_min < min_ts:
                min_ts = rg_min
            if max_ts is None or rg_max > max_ts:
                max_ts = rg_max

        if min_ts is None or max_ts is None:
            log.warning("parquet_footer_no_stats", path=str(path))
            return None

        return PartitionFooter(
            min_ts=min_ts,
            max_ts=max_ts,
            row_count=int(meta.num_rows),
            file_mtime=stat.st_mtime,
            file_size=stat.st_size,
        )
    except (OSError, pa.ArrowInvalid) as exc:  # pragma: no cover — defensive
        log.warning("parquet_footer_read_failed", path=str(path), error=str(exc))
        return None


def _coerce_datetime(value: object) -> datetime:
    """Coerce a parquet stats value (datetime, pd.Timestamp, int ns)
    to a tz-aware UTC ``datetime``. Naive values are interpreted as
    UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    # pyarrow may surface int64 nanoseconds for timestamp[ns]
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1e9, tz=UTC)
    raise TypeError(f"unsupported parquet stats value type: {type(value)!r}")


@dataclass(frozen=True, slots=True)
class PartitionRow:
    """In-memory representation of a ``parquet_partition_index`` row."""

    asset_class: str
    symbol: str
    year: int
    month: int
    min_ts: datetime
    max_ts: datetime
    row_count: int
    file_mtime: float
    file_size: int
    file_path: str


class PartitionIndexGatewayProto(Protocol):
    """Narrow gateway the service depends on. Real implementation lives
    in :mod:`msai.services.symbol_onboarding.partition_index_db`; tests
    pass an :class:`unittest.mock.AsyncMock`. Keeps the service file
    free of SQLAlchemy boilerplate so it stays small."""

    async def fetch_one(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
    ) -> PartitionRow | None: ...

    async def fetch_many(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]: ...

    async def upsert(self, row: PartitionRow) -> None: ...


class PartitionIndexService:
    """Reads + writes the ``parquet_partition_index`` cache.

    Read path:
        1. ``fetch_one`` from cache.
        2. If file missing → return ``None``.
        3. If no cache row → read footer, upsert, return.
        4. If cached ``(mtime, size)`` matches on-disk file → return cached.
        5. Else (file mutated) → re-read footer, upsert, return.

    Write path:
        ``refresh_for_partition`` is called by ``ParquetStore.write_bars``
        unconditionally after each successful atomic write.
    """

    def __init__(self, *, db_gateway: PartitionIndexGatewayProto) -> None:
        self._db = db_gateway

    async def get(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        cached = await self._db.fetch_one(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
        )

        if not path.is_file():
            return None

        stat = path.stat()
        if (
            cached is not None
            and cached.file_mtime == stat.st_mtime
            and cached.file_size == stat.st_size
        ):
            return cached

        return await self._refresh(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            path=path,
        )

    async def get_for_symbol(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]:
        """All cached rows for a symbol, sorted ``(year, month)`` ascending.
        Used by ``compute_coverage`` to assemble the full covered-day set
        in one DB round-trip."""
        rows = await self._db.fetch_many(asset_class=asset_class, symbol=symbol)
        return sorted(rows, key=lambda r: (r.year, r.month))

    async def refresh_for_partition(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        """Force a footer re-read + upsert. Called by ``ParquetStore``
        after each successful write."""
        return await self._refresh(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            path=path,
        )

    async def _refresh(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        path: Path,
    ) -> PartitionRow | None:
        footer = read_parquet_footer(path)
        if footer is None:
            return None
        row = PartitionRow(
            asset_class=asset_class,
            symbol=symbol,
            year=year,
            month=month,
            min_ts=footer.min_ts,
            max_ts=footer.max_ts,
            row_count=footer.row_count,
            file_mtime=footer.file_mtime,
            file_size=footer.file_size,
            file_path=str(path),
        )
        await self._db.upsert(row)
        return row


def make_refresh_callback(*, database_url: str) -> Callable[[str, str, int, int, Path], None]:
    """Build a SYNC callback for ``ParquetStore(partition_index_refresh=...)``.

    The callback **always** opens a fresh ``AsyncEngine`` (with ``NullPool``),
    a fresh ``AsyncSession``, runs the refresh to completion via
    ``asyncio.run`` on whatever thread is calling, and disposes the engine
    before returning. The engine and session therefore never cross event
    loops, and we never share the global ``async_session_factory`` engine
    with a fresh loop — addressing the SQLAlchemy "one async engine per
    loop" rule (P1 Codex iteration 3 fix; see
    https://docs.sqlalchemy.org/20/orm/extensions/asyncio.html#using-multiple-asyncio-event-loops).

    **Caller contract:** ``write_bars`` is sync. It MUST be called from one
    of:

    - A truly sync context (CLI script's main, a worker job's sync callback).
      The refresh callback's ``asyncio.run`` simply runs to completion.
    - A sync context obtained from async code via ``asyncio.to_thread`` (the
      ingest worker pattern). The refresh callback's ``asyncio.run`` runs in
      the worker thread and blocks only that thread; the caller's loop stays
      free.

    **Calling ``write_bars`` directly from an async function** (without
    ``asyncio.to_thread``) is unsupported: the refresh callback would raise
    :class:`CacheRefreshMisuseError` because ``asyncio.run`` cannot start a
    new loop on a thread that already has one running. Wrap every async
    call site in ``await asyncio.to_thread(...)`` — see
    ``services/data_ingestion.py:ingest_historical``.

    Per-call cost is one engine create + dispose (~ms-level for an asyncpg
    connection); negligible against the parquet write itself and acceptable
    on the cache-update path (not the hot read path).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from msai.services.symbol_onboarding.partition_index_db import (
        PartitionIndexGateway,
    )

    async def _do_refresh(asset_class: str, symbol: str, year: int, month: int, path: Path) -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            session_maker = async_sessionmaker(engine, class_=AsyncSession)
            async with session_maker() as session:
                gateway = PartitionIndexGateway(session=session)
                service = PartitionIndexService(db_gateway=gateway)
                await service.refresh_for_partition(
                    asset_class=asset_class,
                    symbol=symbol,
                    year=year,
                    month=month,
                    path=path,
                )
        finally:
            await engine.dispose()

    def _callback(asset_class: str, symbol: str, year: int, month: int, path: Path) -> None:
        # Defensive guard: refuse to run inside an already-running loop
        # (would RuntimeError on asyncio.run). Raise the custom class so
        # the writer's outer try/except (Task 4 Step 4) can let
        # contract-violation errors propagate instead of swallowing them
        # as transient failures (P2 Codex iteration 4 fix).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # No loop on this thread — safe to proceed.
        else:
            raise CacheRefreshMisuseError(
                "make_refresh_callback's callback was invoked from inside a "
                "running event loop. write_bars must be called from a sync "
                "context — wrap async call sites in "
                "`await asyncio.to_thread(store.write_bars, ...)`."
            )

        asyncio.run(_do_refresh(asset_class, symbol, year, month, path))

    return _callback
