from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models import User


def _claim_text(claims: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = claims.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def extract_entra_id(claims: Mapping[str, object]) -> str | None:
    return _claim_text(claims, "oid", "sub")


def extract_email(claims: Mapping[str, object], entra_id: str) -> str:
    return _claim_text(claims, "preferred_username", "email") or f"{entra_id}@msai.local"


def extract_display_name(claims: Mapping[str, object]) -> str | None:
    return _claim_text(claims, "name")


async def ensure_user_from_claims(
    session: AsyncSession,
    claims: Mapping[str, object],
    *,
    default_role: str = "viewer",
) -> User | None:
    entra_id = extract_entra_id(claims)
    if entra_id is None:
        return None

    result = await session.execute(select(User).where(User.entra_id == entra_id))
    user = result.scalar_one_or_none()
    email = extract_email(claims, entra_id)
    display_name = extract_display_name(claims)

    if user is None:
        user = User(
            entra_id=entra_id,
            email=email,
            display_name=display_name,
            role=default_role,
        )
        session.add(user)
        await session.flush()
        return user

    updated = False
    if user.email != email:
        user.email = email
        updated = True
    if display_name and user.display_name != display_name:
        user.display_name = display_name
        updated = True
    if updated:
        await session.flush()

    return user


async def resolve_user_id_from_claims(
    session: AsyncSession,
    claims: Mapping[str, object],
    *,
    default_role: str = "viewer",
) -> str | None:
    user = await ensure_user_from_claims(session, claims, default_role=default_role)
    return user.id if user is not None else None
