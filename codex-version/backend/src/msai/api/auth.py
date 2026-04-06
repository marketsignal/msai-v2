from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def me(
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    entra_id = str(claims.get("oid") or claims.get("sub"))
    email = str(claims.get("preferred_username") or claims.get("email") or "")
    display_name = str(claims.get("name") or "")

    result = await db.execute(select(User).where(User.entra_id == entra_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(entra_id=entra_id, email=email, display_name=display_name)
        db.add(user)
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
