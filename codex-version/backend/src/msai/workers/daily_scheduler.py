from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.core.queue import enqueue_ingestion, get_redis_pool
from msai.services.alerting import alerting_service
from msai.services.daily_universe import DailyUniverseService

logger = get_logger("workers.daily_scheduler")
daily_universe_service = DailyUniverseService()


async def run_scheduler() -> None:
    logger.info(
        "daily_scheduler_started",
        timezone=settings.daily_ingest_timezone,
        hour=settings.daily_ingest_hour,
        minute=settings.daily_ingest_minute,
        enabled=settings.daily_ingest_enabled,
    )
    while True:
        try:
            await enqueue_daily_ingest_if_due()
        except Exception as exc:  # pragma: no cover - defensive loop logging
            alerting_service.send_alert(
                "error",
                "Daily scheduler iteration failed",
                str(exc),
            )
            logger.exception("daily_scheduler_iteration_failed", error=str(exc))
        await asyncio.sleep(settings.daily_ingest_poll_seconds)


async def enqueue_daily_ingest_if_due(now: datetime | None = None) -> bool:
    if not settings.daily_ingest_enabled:
        return False

    zone = ZoneInfo(settings.daily_ingest_timezone)
    current = (now or datetime.now(UTC)).astimezone(zone)
    if not _is_due(current, _load_last_enqueued_date(settings.scheduler_state_path)):
        return False

    end_date = current.date().isoformat()
    start_date = (current.date() - timedelta(days=1)).isoformat()
    pool = await get_redis_pool()
    requests = daily_universe_service.list_requests()
    for request in requests:
        await enqueue_ingestion(
            pool,
            request.asset_class,
            request.symbols,
            start_date,
            end_date,
            provider=request.provider,
            dataset=request.dataset,
            schema=request.schema,
        )
    _write_last_enqueued_date(settings.scheduler_state_path, current.date().isoformat())
    logger.info(
        "daily_ingest_enqueued",
        start_date=start_date,
        end_date=end_date,
        requests=[asdict(request) for request in requests],
    )
    return True


def _is_due(current: datetime, last_enqueued_date: str | None) -> bool:
    scheduled_today = current.replace(
        hour=settings.daily_ingest_hour,
        minute=settings.daily_ingest_minute,
        second=0,
        microsecond=0,
    )
    if current < scheduled_today:
        return False
    return last_enqueued_date != current.date().isoformat()


def _load_last_enqueued_date(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    value = payload.get("last_enqueued_date")
    return str(value) if value else None


def _write_last_enqueued_date(path: Path, run_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_enqueued_date": run_date,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def main() -> None:
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
