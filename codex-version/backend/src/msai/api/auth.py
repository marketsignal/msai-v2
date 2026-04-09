from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.services.user_identity import ensure_user_from_claims

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def me(
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    user = await ensure_user_from_claims(db, claims)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user identity in claims")
    await db.commit()
    await db.refresh(user)

    return {
        "id": user.id,
        "entra_id": user.entra_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }


@router.post("/logout")
async def logout(_: Mapping[str, object] = Depends(get_current_user)) -> dict[str, str]:
    return {"status": "ok", "message": "Session invalidated"}
