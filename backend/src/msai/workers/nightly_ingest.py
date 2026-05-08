"""Nightly data ingestion — arq cron job with tz-aware scheduling.

The ``cron_jobs`` entry in :mod:`msai.workers.settings` fires
:func:`run_nightly_ingest_if_due` every minute (poll). The wrapper
checks ``settings.daily_ingest_enabled`` + a JSON state file to decide
whether to actually run today, then delegates to
:func:`run_nightly_ingest` which does the real ingestion work.

This design (Phase 2 #3 Codex parity port) replaces the previous
hardcoded UTC schedule. Operators can now:

* Set ``DAILY_INGEST_TIMEZONE`` to schedule by local market close (LSE
  16:30 London, TSE 15:00 Tokyo) without a code change.
* Set ``DAILY_INGEST_HOUR`` / ``DAILY_INGEST_MINUTE`` for the local
  trigger time.
* Set ``DAILY_INGEST_ENABLED=false`` to disable without removing the
  cron from the worker.
* Trust the state file for at-most-once-per-day semantics across worker
  restarts and concurrent fires.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

from msai.core.config import settings
from msai.core.logging import get_logger

log = get_logger(__name__)

# Fallback list used only when the asset universe table is empty or the DB
# cannot be reached.  Keeps the worker functional during bootstrap.
_FALLBACK_STOCK_SYMBOLS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "META",
    "TSLA",
    "SPY",
    "QQQ",
    "IWM",
]


def _is_due(current: datetime, last_enqueued_date: str | None) -> bool:
    """Return True when the configured tz local time is past today's
    scheduled hour AND we haven't already ingested for this date.

    ``current`` MUST be timezone-aware in the configured tz (callers
    pass through :func:`zoneinfo.ZoneInfo`). The caller is expected to
    pass the most recent ``last_enqueued_date`` from the state file (or
    ``None`` if no state file exists).
    """
    scheduled = current.replace(
        hour=settings.daily_ingest_hour,
        minute=settings.daily_ingest_minute,
        second=0,
        microsecond=0,
    )
    if current < scheduled:
        return False
    return last_enqueued_date != current.date().isoformat()


def _load_last_enqueued_date(path: Path) -> str | None:
    """Read ``last_enqueued_date`` from the state file, tolerating
    missing/corrupt files (returns ``None`` so the next eligible tick
    will fire and self-heal the file)."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("scheduler_state_corrupt", path=str(path))
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("last_enqueued_date")
    return str(value) if value else None


def _write_last_enqueued_date(path: Path, run_date: str) -> None:
    """Persist the run date so subsequent ticks within the same tz
    calendar day skip the run (idempotency across worker restarts).

    Atomic via tempfile + ``os.replace`` so a worker killed mid-write
    doesn't leave a truncated JSON file — Codex iter 2 P2. Without
    this, the next tick's `_load_last_enqueued_date` would hit a
    ``JSONDecodeError`` and treat state as missing, re-firing today's
    ingest and defeating the idempotency guarantee.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "last_enqueued_date": run_date,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
    fd, tmp_path = tempfile.mkstemp(prefix=".scheduler-", suffix=".json.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise


async def run_nightly_ingest_if_due(
    ctx: dict[str, Any], *, now: datetime | None = None
) -> dict[str, int] | None:
    """Cron entry point — checks tz schedule + idempotency before
    delegating to :func:`run_nightly_ingest`.

    Returns the ingest result dict on a real run, or ``None`` when the
    scheduler decided to skip (disabled, not yet at scheduled time, or
    already ran today).

    ``now`` is exposed for tests; production callers leave it ``None``
    so we read the wall clock.
    """
    if not settings.daily_ingest_enabled:
        # Operator-visible reason at INFO so the disabled state is
        # discoverable in production logs without flipping log levels.
        log.info("daily_ingest_skipped", reason="disabled")
        return None

    try:
        zone = ZoneInfo(settings.daily_ingest_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        log.warning(
            "daily_ingest_invalid_timezone",
            timezone=settings.daily_ingest_timezone,
            error=str(exc),
        )
        return None

    current = (now or datetime.now(UTC)).astimezone(zone)
    last = _load_last_enqueued_date(settings.scheduler_state_path)
    if not _is_due(current, last):
        return None

    # target_date = current.date() + session_offset_days. Default offset 0
    # assumes a post-close same-day schedule (18:00 ET → today's session).
    # Overnight schedules (e.g. 02:00 ET the next morning) set offset=-1
    # to ingest the just-closed prior session — Codex iter 4 P2.
    target_date = current.date() + timedelta(days=settings.daily_ingest_session_offset_days)
    log.info(
        "daily_ingest_firing",
        scheduled_tz=settings.daily_ingest_timezone,
        scheduled_hour=settings.daily_ingest_hour,
        scheduled_minute=settings.daily_ingest_minute,
        session_offset_days=settings.daily_ingest_session_offset_days,
        local_now=current.isoformat(),
        target_date=target_date.isoformat(),
        last_enqueued_date=last,
    )

    # Claim the slot BEFORE running the ingest. arq cron with minute=None
    # fires every minute; if the ingest takes longer than 60s the next
    # tick would re-fire and start a duplicate run. Writing the state
    # eagerly makes `_is_due` return False for subsequent ticks within
    # the same tz day. Trade-off: a transient ingest failure means no
    # auto-retry today (operator can clear the state file or trigger
    # via CLI). Failures are visible via the alerting API (Phase 2 #2),
    # so silent loss is unlikely. Codex iter 1 P2.
    #
    # Record `current.date()` (TODAY in scheduled tz) — NOT target_date.
    # `_is_due` compares the file's value to `current.date()`; with a
    # non-zero session_offset_days, target_date != current.date() and
    # using target_date here would let the idempotency check re-fire
    # the job multiple times on the same cron day.
    _write_last_enqueued_date(settings.scheduler_state_path, current.date().isoformat())
    return await run_nightly_ingest(ctx, target_date=target_date)


async def _load_targets_from_db() -> list[Any] | None:
    """Attempt to load enabled assets from the asset universe table.

    Returns a list of :class:`AssetUniverse` rows, or ``None`` if the
    database is unreachable or the table is empty.
    """
    try:
        from msai.core.database import async_session_factory
        from msai.services.asset_universe import AssetUniverseService

        service = AssetUniverseService()
        async with async_session_factory() as session:
            targets = await service.get_ingest_targets(session)
            if targets:
                return targets
            log.warning("nightly_ingest_empty_universe", fallback="default_symbols")
            return None
    except Exception as exc:
        log.warning("nightly_ingest_db_unavailable", error=str(exc), fallback="default_symbols")
        return None


async def _mark_ingested(targets: list[Any]) -> None:
    """Update last_ingested_at for all ingested assets."""
    try:
        from msai.core.database import async_session_factory
        from msai.services.asset_universe import AssetUniverseService

        service = AssetUniverseService()
        now = datetime.now(UTC)
        async with async_session_factory() as session:
            for asset in targets:
                await service.mark_ingested(session, asset.id, now)
            await session.commit()
    except Exception as exc:
        log.warning("nightly_ingest_mark_failed", error=str(exc))


async def run_nightly_ingest(
    ctx: dict[str, Any], *, target_date: date | None = None
) -> dict[str, int]:
    """Fetch one trading session's bars for all enabled assets in the universe.

    ``target_date`` is the session date to ingest. ``ingest_daily``
    queries ``[target_date, target_date + 1)`` so Databento's end-
    exclusive window returns only that session's bars.

    Defaults to ``yesterday`` (process tz) when called directly (CLI /
    manual trigger), matching the pre-scheduler behavior. The tz-aware
    scheduler wrapper passes ``current.date()`` in the scheduled tz so
    an 18:00 ET run on a UTC host fetches the just-closed US session.

    If the asset universe table is empty or unreachable, falls back to
    the hardcoded default stock symbols so the worker remains functional.
    """
    from msai.services.data_ingestion import DataIngestionService
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.data_sources.polygon_client import PolygonClient
    from msai.services.parquet_store import ParquetStore
    from msai.services.symbol_onboarding.partition_index import make_refresh_callback

    store = ParquetStore(
        str(settings.parquet_root),
        partition_index_refresh=make_refresh_callback(database_url=settings.database_url),
    )
    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None
    databento = DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None
    svc = DataIngestionService(store, polygon=polygon, databento=databento)

    # Try DB-backed universe first
    targets = await _load_targets_from_db()
    combined_result: dict[str, int] = {}

    if targets is not None:
        # Group by asset_class for efficient batching
        groups: dict[str, list[str]] = defaultdict(list)
        target_lookup: dict[str, Any] = {}
        for asset in targets:
            groups[asset.asset_class].append(asset.symbol)
            target_lookup[asset.symbol] = asset

        for asset_class, symbols in groups.items():
            result = await svc.ingest_daily(
                asset_class=asset_class, symbols=symbols, target_date=target_date
            )
            combined_result.update(result)

        # Only mark assets whose ingestion returned non-zero rows as fresh
        successful_targets = [
            asset for asset in targets if combined_result.get(asset.symbol, 0) > 0
        ]
        if successful_targets:
            await _mark_ingested(successful_targets)
        log.info("nightly_ingest_complete", source="database", result=combined_result)
    else:
        # Fallback to hardcoded list
        combined_result = await svc.ingest_daily(
            asset_class="stocks",
            symbols=_FALLBACK_STOCK_SYMBOLS,
            target_date=target_date,
        )
        log.info("nightly_ingest_complete", source="fallback", result=combined_result)

    return combined_result
