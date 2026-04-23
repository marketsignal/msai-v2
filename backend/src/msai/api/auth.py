"""Auth API router -- user identity and session management.

Provides endpoints for retrieving the current user's profile (from JWT claims)
and a placeholder logout endpoint (actual MSAL logout is frontend-driven).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.user import User
from msai.schemas.common import MessageResponse

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class UserMeResponse(MessageResponse):
    """Response schema for GET /me -- extends MessageResponse with user fields."""

    # We use a flat dict approach here for simplicity; the JWT claims plus
    # the DB user ID are returned together.
    pass


@router.get("/me")
async def get_me(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return the current user's profile.

    On first login the user is auto-created in the database using claims
    from the Entra ID JWT (``sub``, ``preferred_username``, ``name``).
    """
    entra_id: str = claims["sub"]
    email: str = claims.get("preferred_username", claims.get("email", "unknown@unknown.com"))
    display_name: str | None = claims.get("name")

    # Look up or auto-create the user record
    result = await db.execute(select(User).where(User.entra_id == entra_id))
    user: User | None = result.scalar_one_or_none()

    if user is None:
        user = User(
            entra_id=entra_id,
            email=email,
            display_name=display_name,
            role="viewer",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        log.info("user_auto_created", user_id=str(user.id), email=email)

    return {
        "id": str(user.id),
        "entra_id": user.entra_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }


@router.post("/logout", response_model=MessageResponse)
async def logout() -> MessageResponse:
    """Placeholder logout endpoint.

    The actual MSAL token revocation is handled by the frontend.
    The backend simply acknowledges the request.
    """
    return MessageResponse(message="Logged out successfully")
