"""System health aggregator API router.

Exposes a single endpoint ``GET /api/v1/system/health`` that aggregates
subsystem statuses (DB, Redis, IB Gateway, worker queues, parquet storage)
plus version + commit SHA + uptime.  The endpoint is designed for the
``/system`` page in the dashboard which polls every 30 s.

Design constraints (per T4 spec):

- DB ping uses ``SELECT 1`` with a short timeout (~500 ms).
- Redis ping uses ``redis.asyncio.Redis.ping()`` wrapped in
  ``asyncio.wait_for(..., timeout=0.5)``.
- IB Gateway status reads the *cached* ``_ib_probe.is_healthy`` so this
  endpoint never blocks on a fresh TCP probe.
- Worker queue depth is read via ``ZCARD`` on each known arq queue
  (arq 0.27 stores pending job ids in a sorted set keyed by queue name).
  ``arq`` stores pending jobs in a Redis list whose key is the queue name.
- Parquet stats come from :meth:`ParquetStore.get_storage_stats` (cheap
  ``stat()`` walk; no Parquet decoding).
- Version is read from ``msai`` package metadata (set by ``pyproject.toml``).
- Commit SHA is captured once at module import time from ``GITHUB_SHA``
  env var (production) or ``git rev-parse --short HEAD`` (local dev).
- Uptime is tracked from ``_APP_START_TIME`` set at module import time.

Per ``.claude/rules/api-design.md`` §"Error Response Format", subsystem
failures return ``status: "unhealthy"`` (or ``"unknown"`` if state cannot
be determined) inline within a 200 response — they are not 5xx errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.parquet_store import ParquetStore

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


# Process-lifetime state — captured once at module import.

_APP_START_TIME: float = time.monotonic()


def _resolve_version() -> str:
    """Read the ``msai`` package version, falling back to ``"unknown"``."""
    try:
        return importlib_metadata.version("msai")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _resolve_commit_sha() -> str:
    """Resolve the deployed git commit SHA (7-char short form)."""
    sha = os.environ.get("GITHUB_SHA", "")
    if sha:
        return sha[:7]
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


_APP_VERSION: str = _resolve_version()
_COMMIT_SHA: str = _resolve_commit_sha()


# Timeouts kept tight; /system page polls every 30 s.
_DB_PING_TIMEOUT_S: float = 0.5
_REDIS_PING_TIMEOUT_S: float = 0.5
_QUEUE_DEPTH_TIMEOUT_S: float = 0.5


class SubsystemStatus(BaseModel):
    """One subsystem's health snapshot."""

    status: str = Field(description="healthy | unhealthy | unknown")
    last_checked: str = Field(description="ISO8601 UTC timestamp of the latest probe")
    detail: str | None = Field(default=None, description="Free-form error/diagnostic detail")

    model_config = {"extra": "allow"}


class SystemHealthResponse(BaseModel):
    """Aggregated subsystem health + build metadata."""

    subsystems: dict[str, SubsystemStatus]
    version: str
    commit_sha: str
    uptime_seconds: int


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_status(
    status: str,
    detail: str | None = None,
    **extras: Any,
) -> SubsystemStatus:
    """Build a :class:`SubsystemStatus` with arbitrary extra fields.

    Routed through ``model_validate`` because ``mypy --strict`` rejects
    arbitrary kwargs on the constructor even with ``model_config =
    {"extra": "allow"}``. The validate path lets Pydantic accept the
    extras at runtime while the type checker only sees a single dict
    argument.
    """
    data: dict[str, Any] = {
        "status": status,
        "last_checked": _now_iso(),
        "detail": detail,
    }
    data.update(extras)
    return SubsystemStatus.model_validate(data)


def _log_probe_failure(event: str, exc: BaseException) -> None:
    """Log a probe-failure event with the canonical (error, error_type) shape.

    iter-5 SF P3 introduced the convention; this helper keeps every probe's
    failure path uniform so a real outage leaves a forensic trail without
    repeating the log-call shape five times.
    """
    log.warning(event, error=str(exc), error_type=type(exc).__name__)


async def _probe_db() -> SubsystemStatus:
    """Ping PostgreSQL via SELECT 1 with a short timeout."""
    from msai.core.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=_DB_PING_TIMEOUT_S,
            )
    except TimeoutError:
        return SubsystemStatus(
            status="unhealthy",
            last_checked=_now_iso(),
            detail=f"timeout after {_DB_PING_TIMEOUT_S}s",
        )
    except Exception as exc:  # noqa: BLE001
        _log_probe_failure("system_health_db_probe_failed", exc)
        return SubsystemStatus(
            status="unhealthy",
            last_checked=_now_iso(),
            detail=str(exc)[:200],
        )
    return SubsystemStatus(status="healthy", last_checked=_now_iso(), detail=None)


async def _probe_redis() -> SubsystemStatus:
    """Ping Redis with PING, bounded by _REDIS_PING_TIMEOUT_S."""
    from redis.asyncio import Redis as AsyncRedis

    client: AsyncRedis | None = None
    try:
        client = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
        await asyncio.wait_for(client.ping(), timeout=_REDIS_PING_TIMEOUT_S)
    except TimeoutError:
        return SubsystemStatus(
            status="unhealthy",
            last_checked=_now_iso(),
            detail=f"timeout after {_REDIS_PING_TIMEOUT_S}s",
        )
    except Exception as exc:  # noqa: BLE001
        _log_probe_failure("system_health_redis_probe_failed", exc)
        return SubsystemStatus(
            status="unhealthy",
            last_checked=_now_iso(),
            detail=str(exc)[:200],
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
    return SubsystemStatus(status="healthy", last_checked=_now_iso(), detail=None)


def _probe_ib_gateway() -> SubsystemStatus:
    """Read the cached IBProbe state — no fresh I/O on this path.

    If the probe loop hasn't started yet, return ``unknown`` rather
    than ``unhealthy`` (absence of data != broken).
    """
    try:
        from msai.api.account import _ib_probe, _probe_task
    except Exception as exc:  # noqa: BLE001
        # A module-import failure at runtime is a real programming bug
        # (renamed symbol, circular import). Log so it doesn't hide behind
        # an "unknown" status forever. iter-5 SF P3.
        _log_probe_failure("system_health_ib_probe_import_failed", exc)
        return SubsystemStatus(
            status="unknown",
            last_checked=_now_iso(),
            detail=f"ib_probe module unavailable: {str(exc)[:200]}",
        )

    if _probe_task is None or _probe_task.done():
        return SubsystemStatus(
            status="unknown",
            last_checked=_now_iso(),
            detail="probe loop not running",
        )

    if _ib_probe.is_healthy:
        return SubsystemStatus(status="healthy", last_checked=_now_iso(), detail=None)
    return SubsystemStatus(
        status="unhealthy",
        last_checked=_now_iso(),
        detail=f"consecutive_failures={_ib_probe.consecutive_failures}",
    )


# arq stores pending jobs as Redis lists keyed by queue name.
_WORKER_QUEUES: tuple[str, ...] = (
    "arq:queue",
    "msai:research",
    "msai:portfolio",
    "msai:ingest",
)


async def _probe_workers() -> SubsystemStatus:
    """Aggregate queue depth across every known arq queue.

    arq 0.27 stores pending job ids in a sorted set keyed by queue name
    (``zadd``/``zcard``). LLEN against that key raises WRONGTYPE — Codex
    iter-1 P2 caught the prior LLEN implementation reporting workers as
    ``unknown`` with depth 0 EXACTLY when there was work to do.

    Best-effort: a Redis outage degrades to ``unhealthy`` (not
    ``unknown``); ``unknown`` is reserved for "probe loop not started"
    elsewhere (e.g., IB Gateway).
    """
    from redis.asyncio import Redis as AsyncRedis

    client: AsyncRedis | None = None
    try:
        client = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
        # ``client.zcard`` is typed as ``Awaitable[int] | int | Any`` by the
        # redis-py stubs (covers sync + async clients); on the async client
        # it is always awaitable. Wrap each call in a ``cast`` so mypy
        # accepts the gather + so asyncio can schedule the coroutines.
        from typing import cast

        coros: list[Any] = [cast("Any", client.zcard(q)) for q in _WORKER_QUEUES]
        depths = await asyncio.wait_for(
            asyncio.gather(*coros),
            timeout=_QUEUE_DEPTH_TIMEOUT_S,
        )
    except TimeoutError:
        return _make_status(
            "unhealthy",
            f"queue probe timeout after {_QUEUE_DEPTH_TIMEOUT_S}s",
        )
    except Exception as exc:  # noqa: BLE001
        _log_probe_failure("system_health_workers_probe_failed", exc)
        return _make_status("unhealthy", str(exc)[:200])
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    total_depth = sum(int(d) for d in depths)
    return _make_status("healthy", None, queue_depth=total_depth)


def _probe_parquet() -> SubsystemStatus:
    """Return total Parquet file count + total bytes via stat() walk."""
    try:
        store = ParquetStore(str(settings.parquet_root))
        stats = store.get_storage_stats()
    except Exception as exc:  # noqa: BLE001
        _log_probe_failure("system_health_parquet_probe_failed", exc)
        return _make_status(
            "unhealthy",
            str(exc)[:200],
            total_files=0,
            total_bytes=0,
        )
    return _make_status(
        "healthy",
        None,
        total_files=int(stats.get("total_files", 0)),
        total_bytes=int(stats.get("total_bytes", 0)),
    )


@router.get("/health", response_model=SystemHealthResponse)
async def system_health(
    _: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> SystemHealthResponse:
    """Aggregate subsystem health into a single payload."""
    db_task = _probe_db()
    redis_task = _probe_redis()
    workers_task = _probe_workers()

    db_status, redis_status, workers_status = await asyncio.gather(
        db_task, redis_task, workers_task
    )

    subsystems: dict[str, SubsystemStatus] = {
        "api": SubsystemStatus(status="healthy", last_checked=_now_iso(), detail=None),
        "db": db_status,
        "redis": redis_status,
        "ib_gateway": _probe_ib_gateway(),
        "workers": workers_status,
        "parquet": _probe_parquet(),
    }

    uptime = max(0, int(time.monotonic() - _APP_START_TIME))

    return SystemHealthResponse(
        subsystems=subsystems,
        version=_APP_VERSION,
        commit_sha=_COMMIT_SHA,
        uptime_seconds=uptime,
    )
