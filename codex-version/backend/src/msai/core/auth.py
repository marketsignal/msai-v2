"""Authentication middleware supporting both Azure Entra ID JWT and API key.

- Bearer token: validated against Entra ID JWKS (for frontend browser flow)
- X-API-Key header: validated against MSAI_API_KEY env var (for CLI, testing, scripts)
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from msai.core.config import settings

_bearer = HTTPBearer(auto_error=False)

_API_KEY_CLAIMS: dict[str, Any] = {
    "sub": "api-key-user",
    "preferred_username": "api-key@msai.local",
    "name": "API Key User",
}


class EntraIDValidator:
    def __init__(self, tenant_id: str, client_id: str) -> None:
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._audience = client_id
        self._jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        )

    def validate_token(self, token: str) -> Mapping[str, Any]:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )


@lru_cache
def get_token_validator() -> EntraIDValidator:
    return EntraIDValidator(settings.jwt_tenant_id, settings.jwt_client_id)


def validate_api_key(key: str) -> bool:
    """Check if the provided API key matches the configured MSAI_API_KEY."""
    return bool(settings.msai_api_key) and key == settings.msai_api_key


def validate_token_or_api_key(token: str) -> Mapping[str, Any]:
    """Validate a token string as either an API key or JWT.

    Used by WebSocket auth where the first message is the auth credential.
    """
    if validate_api_key(token.strip()):
        return _API_KEY_CLAIMS

    return get_token_validator().validate_token(token.strip())


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Mapping[str, Any]:
    """FastAPI dependency: authenticate via X-API-Key header or Bearer token."""
    api_key = request.headers.get("X-API-Key")
    if api_key and validate_api_key(api_key):
        return _API_KEY_CLAIMS

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header or X-API-Key",
        )

    try:
        return get_token_validator().validate_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc
