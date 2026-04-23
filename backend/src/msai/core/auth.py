"""Authentication middleware supporting both Azure Entra ID JWT and API key.

- Bearer token: validated against Entra ID JWKS (for frontend browser flow)
- X-API-Key header: validated against MSAI_API_KEY env var (for CLI, testing, scripts)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from msai.core.config import settings
from msai.core.logging import get_logger

log = get_logger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

_API_KEY_CLAIMS: dict[str, Any] = {
    "sub": "api-key-user",
    "preferred_username": "api-key@msai.local",
    "name": "API Key User",
}


class EntraIDValidator:
    """Validates JWTs issued by Azure Entra ID using JWKS key discovery."""

    def __init__(self, tenant_id: str, client_id: str) -> None:
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._audience = client_id
        self._jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_jwk_set=True,
            lifespan=300,
        )

    def validate_token(self, token: str) -> dict[str, Any]:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
        return payload


_validator: EntraIDValidator | None = None


def init_validator(tenant_id: str, client_id: str) -> EntraIDValidator:
    global _validator  # noqa: PLW0603
    _validator = EntraIDValidator(tenant_id, client_id)
    return _validator


def get_validator() -> EntraIDValidator | None:
    """Return the validator, or None if Entra ID is not configured."""
    return _validator


def validate_api_key(key: str) -> bool:
    """Check if the provided API key matches the configured MSAI_API_KEY."""
    return bool(settings.msai_api_key) and key == settings.msai_api_key


def validate_token_or_api_key(token: str) -> dict[str, Any]:
    """Validate a token string as either an API key or JWT.

    Used by WebSocket auth where the first message is the auth credential.
    """
    if validate_api_key(token.strip()):
        return _API_KEY_CLAIMS

    if _validator is None:
        raise jwt.InvalidTokenError("Authentication not configured")

    return _validator.validate_token(token.strip())


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> dict[str, Any]:
    """FastAPI dependency: authenticate via X-API-Key header or Bearer token.

    Checks X-API-Key first, then falls back to Bearer JWT validation.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key and validate_api_key(api_key):
        return _API_KEY_CLAIMS

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header or X-API-Key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    validator = get_validator()
    if validator is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication not configured. Set AZURE_TENANT_ID and AZURE_CLIENT_ID, or MSAI_API_KEY.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload: dict[str, Any] = validator.validate_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return payload


async def get_current_user_or_none(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> dict[str, Any] | None:
    """Same as ``get_current_user`` but returns ``None`` on missing auth.

    Use ONLY when the caller has a fallback auth path (e.g., a signed
    token in a query string). Returning ``None`` instead of raising 401
    lets the caller decide whether the fallback is sufficient; without
    this helper every dependency would short-circuit the request with
    a 401 before the handler could inspect the alternative credential.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key is None and credentials is None:
        return None
    try:
        return await get_current_user(request=request, credentials=credentials)
    except HTTPException as exc:
        # A credential WAS presented but failed validation. Log to distinguish
        # a misconfigured Entra tenant / expired JWT / bad API key from the
        # legitimate "iframe fetch, no session" path above.
        log.info(
            "auth_optional_declined",
            detail=str(exc.detail),
            status_code=exc.status_code,
        )
        return None


async def resolve_user_id(
    db: AsyncSession,
    claims: dict[str, Any],
) -> UUID | None:
    """Resolve JWT claims to a ``users.id`` UUID, auto-creating the row if needed.

    The ``sub`` claim from Entra ID (or the API-key sentinel) is an opaque
    string, but FK columns (``promoted_by``, ``transitioned_by``, etc.)
    reference ``users.id`` (UUID).  This helper bridges the gap.

    Concurrency: a concurrent insert losing the race is handled inside a
    SAVEPOINT so the outer transaction's ORM objects are preserved.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from msai.models.user import User

    sub = claims.get("sub")
    if not sub:
        return None

    result = await db.execute(select(User.id).where(User.entra_id == sub))
    row_id = result.scalar_one_or_none()
    if row_id is not None:
        return row_id

    email = claims.get("preferred_username") or f"{sub}@unknown.local"
    new_user = User(
        entra_id=str(sub),
        email=str(email),
        display_name=claims.get("name"),
        role="operator",
    )
    try:
        async with db.begin_nested():
            db.add(new_user)
            await db.flush()
    except IntegrityError:
        result = await db.execute(select(User.id).where(User.entra_id == sub))
        return result.scalar_one_or_none()
    return new_user.id
