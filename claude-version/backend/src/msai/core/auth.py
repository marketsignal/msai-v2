"""JWT validation middleware for Azure Entra ID (formerly Azure AD).

Uses PyJWT with JWKS endpoint discovery to validate RS256-signed tokens
issued by Microsoft identity platform v2.0.
"""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

_bearer_scheme = HTTPBearer(auto_error=False)


class EntraIDValidator:
    """Validates JWTs issued by Azure Entra ID using JWKS key discovery.

    Args:
        tenant_id: Azure AD tenant ID (GUID).
        client_id: Application (client) ID registered in Entra ID.
    """

    def __init__(self, tenant_id: str, client_id: str) -> None:
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._audience = client_id
        self._jwks_client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_jwk_set=True,
            lifespan=300,
        )

    def validate_token(self, token: str) -> dict[str, Any]:
        """Decode and validate a JWT against Azure Entra ID.

        Verifies the token signature using the JWKS endpoint, checks the
        ``exp``, ``iss``, ``aud``, and ``sub`` claims.

        Args:
            token: Raw JWT string (without "Bearer " prefix).

        Returns:
            Decoded payload as a dictionary.

        Raises:
            jwt.InvalidTokenError: If the token is expired, has a wrong
                audience/issuer, is missing required claims, or has an
                invalid signature.
        """
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


# ---------------------------------------------------------------------------
# Module-level validator instance (set by application startup)
# ---------------------------------------------------------------------------

_validator: EntraIDValidator | None = None


def init_validator(tenant_id: str, client_id: str) -> EntraIDValidator:
    """Initialise the module-level EntraIDValidator.

    Call this once during application startup (e.g. in a FastAPI lifespan).

    Returns:
        The newly created validator instance.
    """
    global _validator  # noqa: PLW0603
    _validator = EntraIDValidator(tenant_id, client_id)
    return _validator


def get_validator() -> EntraIDValidator:
    """Return the module-level validator, raising 401 if not initialised."""
    if _validator is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication not configured. Set AZURE_TENANT_ID and AZURE_CLIENT_ID.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _validator


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    validator: EntraIDValidator = Depends(get_validator),  # noqa: B008
) -> dict[str, Any]:
    """FastAPI dependency that extracts and validates the Bearer token.

    Returns:
        Decoded JWT claims dictionary containing at least ``sub``, ``iss``,
        ``aud``, and ``exp``.

    Raises:
        HTTPException: 401 if the Authorization header is missing, malformed,
            or the token fails validation.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
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
