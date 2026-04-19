"""Audit logging for tracking all mutations (POST/PATCH/DELETE).

Provides a single ``log_audit`` coroutine that records every state-changing
operation performed by a user.  The entry is emitted immediately via structlog
for observability and will be persisted to the ``audit_log`` table once the
database model is available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)


async def log_audit(
    db: AsyncSession,
    user_id: UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit log entry to the database.

    Also logs via structlog for immediate visibility.
    For now, only logs via structlog since the ``AuditLog`` model
    will be available after the models migration is created.

    Args:
        db: The async database session (used for future DB persistence).
        user_id: Authenticated user who triggered the action, or ``None``
            for unauthenticated / system-initiated operations.
        action: A short verb describing the mutation (e.g. ``"create"``,
            ``"update"``, ``"delete"``).
        resource_type: The kind of resource affected (e.g. ``"strategy"``,
            ``"backtest"``).
        resource_id: Primary key of the affected resource, if applicable.
        details: Arbitrary metadata to attach to the log entry.
    """
    log.info(
        "audit",
        user_id=str(user_id) if user_id else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        details=details,
    )
    # TODO: Insert into audit_log table once AuditLog model exists
    # from msai.models.audit_log import AuditLog
    # entry = AuditLog(user_id=user_id, action=action, ...)
    # db.add(entry)
    # await db.flush()
