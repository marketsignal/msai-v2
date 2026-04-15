"""Operational alerting for MSAI v2.

Two cooperating surfaces:

* :class:`AlertingService` — file-backed history store (ported from the
  Codex version) that powers the ``GET /api/v1/alerts/`` audit trail for
  the dashboard. Cap at 200 records, newest first.
* :class:`AlertService` — async SMTP sender used by the live supervisor
  and disconnect handler. Its ``send_alert`` also records history through
  the shared module-level :data:`alerting_service` singleton so every
  alert is visible in the API regardless of SMTP success or configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import smtplib
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING

from msai.core.config import settings
from msai.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

log = get_logger(__name__)

_MAX_HISTORY = 200

# Upper bound on how long `AlertService.send_alert` will wait for the
# file-backed history write. In the normal case the write takes single-
# digit milliseconds (flock + small JSON + fsync). If the alerts volume
# is wedged (hardware failure, bind-mount gone), we cap the wait so
# critical call sites like `_mark_failed` and `_fire_halt` can proceed.
_HISTORY_WRITE_TIMEOUT_S = 2.0

# Dedicated single-thread executor for history writes. Isolated from
# asyncio's default executor so a wedged alerts volume can never saturate
# the pool that SMTP and other `run_in_executor` callers share (Codex
# iter 9 P1). Max workers = 1 because writes already serialise on
# `fcntl.flock`, so extra threads only add noise.
_HISTORY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alert-history")

# Map an alert's user-level severity to a structlog log method so the
# backend log line ends up at the matching level. Structlog's
# `add_log_level` processor derives the log level from the method name
# (log.warning → "warning"), so passing ``level=...`` as a kwarg would
# collide; we rename it to ``alert_level`` in the structured record.
_LOG_METHOD_BY_ALERT_LEVEL = {
    "debug": "debug",
    "info": "info",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}


_REQUIRED_ALERT_FIELDS = ("type", "level", "title", "message", "created_at")


def _coerce_alerts_list(raw: object) -> list[dict[str, str]]:
    """Coerce an untrusted payload's ``alerts`` field into a list.

    Guards against operator hand-edits that put a non-list under the
    ``alerts`` key (e.g. ``{"alerts": "oops"}`` — iterating a string would
    yield one-char entries — or ``{"alerts": 42}`` — ``list()`` raises
    ``TypeError``). Non-dict entries are silently dropped so a single bad
    row doesn't poison the whole file.
    """
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _is_valid_alert_entry(entry: Mapping[str, object]) -> bool:
    """True if the row has all required string fields.

    Applied in ``list_alerts`` so that malformed rows are dropped before
    the ``limit`` slice — otherwise a bad entry among the newest N
    records would hide older valid alerts behind the limit cap.
    """
    return all(field in entry and isinstance(entry[field], str) for field in _REQUIRED_ALERT_FIELDS)


def _valid_alerts(payload: Mapping[str, object]) -> list[dict[str, str]]:
    """Return only structurally valid alert rows from a decoded payload.

    Shared by ``list_alerts`` (filter before slicing to ``limit``) and
    ``_write_event`` (filter before slicing to ``_MAX_HISTORY``) so both
    paths enforce the same row-level invariant.
    """
    return [
        entry
        for entry in _coerce_alerts_list(payload.get("alerts"))
        if _is_valid_alert_entry(entry)
    ]


class AlertingService:
    """File-backed alert history store.

    Ported from ``codex-version/backend/src/msai/services/alerting.py``. The
    store is a JSON file with a single ``alerts`` array, newest first,
    capped at :data:`_MAX_HISTORY` entries.

    The module-level :data:`alerting_service` singleton is the expected
    access point across workers and the API router.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.alerts_path

    def send_alert(self, level: str, title: str, message: str) -> None:
        self._write_event("alert", level=level, title=title, message=message)
        method_name = _LOG_METHOD_BY_ALERT_LEVEL.get(level, "warning")
        log_method = getattr(log, method_name)
        # `alert_level` (not `level`) to avoid colliding with structlog's
        # add_log_level processor, which always injects its own `level` key.
        log_method("alert", alert_level=level, title=title, message=message)

    def send_recovery(self, title: str, message: str) -> None:
        self._write_event("recovery", level="info", title=title, message=message)
        log.info("alert_recovery", title=title, message=message)

    def list_alerts(self, *, limit: int = 50) -> list[dict[str, str]]:
        # Take the same exclusive lock as writers. Linux `flock` is not a
        # fair scheduler, and a dashboard polling this endpoint from N
        # workers with LOCK_SH could starve a pending LOCK_EX writer. The
        # audit log is low-throughput so a read queuing behind a write
        # (and vice versa) has no practical cost, and uniform locking is
        # easier to reason about.
        if not self.path.exists():
            return []
        # Lock is best-effort on the read path: on a read-only volume,
        # opening the lockfile in "a" mode raises PermissionError, but the
        # history file itself may still be readable and operators need
        # that audit trail exactly when storage is degraded. Fall back to
        # an unlocked read and accept a (very unlikely, given writes are
        # blocked anyway on a read-only volume) torn view.
        try:
            with self._locked():
                payload = self._read_payload()
        except OSError:
            # Covers PermissionError (read-only volume), FileNotFoundError
            # (lock dir removed), and other degraded-storage conditions.
            log.warning("alerts_read_lock_unavailable_falling_back")
            payload = self._read_payload()
        # Filter malformed rows before applying the limit so a stale bad
        # top entry doesn't hide valid older alerts behind a small limit.
        return _valid_alerts(payload)[:limit]

    def _write_event(self, event_type: str, *, level: str, title: str, message: str) -> None:
        # Serialised read-modify-write. fcntl.flock on a lockfile sidecar
        # gives us a cross-process advisory lock on POSIX so two workers
        # emitting alerts simultaneously can't clobber each other. The
        # atomic replace inside the lock guarantees readers either see the
        # previous state or the new state, never a torn file.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            current = self._read_payload() if self.path.exists() else {"alerts": []}
            # Drop malformed rows BEFORE applying the retention cap so a
            # stray hand-edited bad row doesn't permanently consume one of
            # the 200 retained slots and evict a real alert early.
            alerts = _valid_alerts(current)
            alerts.insert(
                0,
                {
                    "type": event_type,
                    "level": level,
                    "title": title,
                    "message": message,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            payload = json.dumps({"alerts": alerts[:_MAX_HISTORY]}, indent=2, sort_keys=True)

            fd, tmp_path = tempfile.mkstemp(
                prefix=".alerts-", suffix=".json.tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.path)
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)
                raise

    def _read_payload(self) -> dict[str, list[dict[str, str]]]:
        """Decode the alerts file, self-healing on unexpected shapes.

        Two failure modes are tolerated: invalid JSON (``JSONDecodeError``)
        and valid-but-wrong-shape JSON (``[]``, ``"oops"``, ``null``, …).
        In both cases we fall back to an empty history so ``list_alerts``
        doesn't 500 and ``_write_event`` starts fresh on the next insert.
        Callers must already hold the file lock.
        """
        try:
            decoded = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"alerts": []}
        if not isinstance(decoded, dict):
            return {"alerts": []}
        return decoded

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive fcntl advisory lock on ``{alerts_path}.lock``.

        Both readers and writers take LOCK_EX so the lock acts as a simple
        mutex — readers + writers queue against one another without
        starvation risk. POSIX-only; Docker deployment guarantees Linux.

        The sidecar lock file is intentionally persistent and never
        unlinked: a concurrent holder's flock state lives on its open fd,
        and removing the inode mid-lock would race with the next opener.
        One lockfile per service path is a negligible footprint.
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock_fp:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


alerting_service = AlertingService()


def _log_history_failure(fut: asyncio.Future[None], subject: str) -> None:
    """Done-callback for the fire-and-forget history write.

    ``add_done_callback`` swallows nothing — if we don't consume the
    exception here the task is reported as "never awaited" at garbage
    collection and the error is effectively silent. We pull the exception
    and log it.
    """
    exc = fut.exception()
    if exc is not None:
        log.warning(
            "alert_history_write_failed",
            subject=subject,
            error=repr(exc),
        )


class AlertService:
    """Send operational email alerts via SMTP.

    When ``smtp_host`` is empty the service degrades gracefully: alerts are
    logged as warnings but never sent.  This allows the rest of the
    application to call alerting methods unconditionally without crashing
    in environments where SMTP is not configured.

    Every alert is also recorded in :data:`alerting_service` history so it
    surfaces in ``GET /api/v1/alerts/`` even when email fails or is
    unconfigured.
    """

    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        sender: str = "",
        password: str = "",
        default_recipients: list[str] | None = None,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.password = password
        self.default_recipients = default_recipients or []
        # Last fire-and-forget history write. Exposed so tests can await
        # completion without racing against the executor; callers should
        # never rely on this — it's best-effort by design.
        self.last_history_task: asyncio.Future[None] | None = None

    async def send_alert(
        self,
        subject: str,
        body: str,
        recipients: list[str] | None = None,
        *,
        level: str = "warning",
    ) -> bool:
        """Send an email alert. Returns ``True`` on SMTP success.

        History is recorded best-effort on top of SMTP: a read-only volume
        or a full disk must not disable operational notifications, so a
        failure in the history write logs and continues. The happy path
        still produces an auditable entry in ``GET /api/v1/alerts/``.
        """
        # History is best-effort. Offload the sync file I/O (flock +
        # fsync) to the executor so the event loop is not stalled CPU-
        # wise, AND await completion so short-lived callers (IB
        # disconnect subprocess shutdown, process manager failure paths)
        # get persistence before returning. A hard timeout bounds the
        # worst case: a wedged alerts volume logs + continues rather
        # than hanging the caller forever (Codex iter 7/8 synthesis).
        loop = asyncio.get_running_loop()
        history_task = loop.run_in_executor(
            _HISTORY_EXECUTOR, alerting_service.send_alert, level, subject, body
        )
        self.last_history_task = history_task
        try:
            await asyncio.wait_for(asyncio.shield(history_task), timeout=_HISTORY_WRITE_TIMEOUT_S)
        except TimeoutError:
            log.warning(
                "alert_history_write_timed_out",
                subject=subject,
                timeout_s=_HISTORY_WRITE_TIMEOUT_S,
            )
            # Don't leak the background task — attach the done-callback
            # so the eventual completion (or exception) is consumed.
            history_task.add_done_callback(lambda fut: _log_history_failure(fut, subject))
        except Exception:
            log.exception("alert_history_write_failed", subject=subject)

        to = recipients or self.default_recipients
        if not self.smtp_host:
            log.warning("alert_not_sent_no_smtp", subject=subject)
            return False

        if not to:
            log.warning("alert_not_sent_no_recipients", subject=subject)
            return False

        try:
            msg = EmailMessage()
            msg["Subject"] = f"[MSAI Alert] {subject}"
            msg["From"] = self.sender
            msg["To"] = ", ".join(to)
            msg.set_content(body)

            await loop.run_in_executor(None, self._send_smtp, msg)

            log.info("alert_sent", subject=subject, recipients=to)
            return True
        except Exception:
            log.exception("alert_send_failed", subject=subject)
            return False

    def _send_smtp(self, msg: EmailMessage) -> None:
        """Send an email message via SMTP synchronously.

        Intended to be called from :meth:`send_alert` inside
        ``loop.run_in_executor`` so the event loop is not blocked.
        """
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.sender, self.password)
            server.send_message(msg)

    async def alert_strategy_error(self, strategy_name: str, error: str) -> None:
        """Alert when a strategy raises an unexpected error."""
        await self.send_alert(
            f"Strategy Error: {strategy_name}",
            f"Strategy '{strategy_name}' encountered an error:\n\n{error}",
            level="error",
        )

    async def alert_daily_loss(self, current_pnl: float, threshold: float) -> None:
        """Alert when the daily P&L breaches the configured loss threshold."""
        await self.send_alert(
            "Daily Loss Threshold Breached",
            f"Current P&L: ${current_pnl:,.2f}\nThreshold: ${threshold:,.2f}",
            level="critical",
        )

    async def alert_system_down(self, service: str) -> None:
        """Alert when a critical service stops responding."""
        await self.send_alert(
            f"Service Down: {service}",
            f"{service} is not responding.",
            level="critical",
        )

    async def alert_ib_disconnect(self) -> None:
        """Alert when the IB Gateway connection is lost."""
        await self.send_alert(
            "IB Gateway Disconnected",
            "Interactive Brokers Gateway lost connection. "
            "Check the ib-gateway-troubleshooting runbook for resolution steps.",
            level="critical",
        )
