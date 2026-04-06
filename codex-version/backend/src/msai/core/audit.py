from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models import AuditLog

_MUTATING_METHODS = {"POST", "PATCH", "DELETE"}
logger = get_logger("audit")


def _extract_resource_id(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    candidate = parts[-1]
    if "{" in candidate or "}" in candidate:
        return None
    return candidate


async def record_audit_event(
    session: AsyncSession,
    user_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    details: dict | None,
) -> None:
    session.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
        )
    )
    await session.commit()


async def audit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)

    if request.method not in _MUTATING_METHODS or not request.url.path.startswith("/api/v1/"):
        return response

    claims = getattr(request.state, "user", None)
    user_id = None
    if isinstance(claims, dict):
        user_id = claims.get("oid") or claims.get("sub")

    try:
        async with async_session_factory() as session:
            await record_audit_event(
                session=session,
                user_id=user_id,
                action=f"{request.method.lower()}:{request.url.path}",
                resource_type=request.url.path.split("/")[3] if len(request.url.path.split("/")) > 3 else None,
                resource_id=_extract_resource_id(request.url.path),
                details={"status_code": response.status_code},
            )
    except Exception as exc:
        logger.warning("audit_write_failed", error=str(exc), path=request.url.path)

    return response
