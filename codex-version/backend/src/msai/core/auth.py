from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from msai.core.config import settings

_bearer = HTTPBearer(auto_error=False)


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


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Mapping[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        return get_token_validator().validate_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
