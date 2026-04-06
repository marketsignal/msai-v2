"""Unit tests for Azure Entra ID JWT validation middleware."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from msai.core.auth import EntraIDValidator, get_current_user, get_validator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TENANT_ID = "00000000-0000-0000-0000-000000000000"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"


def _private_key_to_pem(private_key: rsa.RSAPrivateKey) -> bytes:
    """Serialise an RSA private key to PEM bytes."""
    return private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )


@pytest.fixture()
def rsa_keypair() -> tuple[rsa.RSAPrivateKey, bytes]:
    """Generate an ephemeral RSA key pair for signing test tokens."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_key_pem


def _make_token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = "user-123",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    exp_offset: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Encode a JWT signed with the given RSA private key."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
    }
    if extra_claims:
        payload.update(extra_claims)

    private_pem = _private_key_to_pem(private_key)
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key-1"})


def _make_token_without_sub(
    private_key: rsa.RSAPrivateKey,
) -> str:
    """Encode a JWT that intentionally omits the 'sub' claim."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    private_pem = _private_key_to_pem(private_key)
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key-1"})


def _mock_jwks_client(public_key_pem: bytes) -> MagicMock:
    """Create a mock PyJWKClient that returns the provided public key."""
    mock_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key_pem
    mock_client.get_signing_key_from_jwt.return_value = mock_signing_key
    return mock_client


def _create_test_app(validator: EntraIDValidator) -> FastAPI:
    """Build a minimal FastAPI app wired with the auth dependency."""
    app = FastAPI()

    def _override_get_validator() -> EntraIDValidator:
        return validator

    app.dependency_overrides[get_validator] = _override_get_validator

    @app.get("/me")
    async def me(
        user: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, Any]:
        return user

    return app


# ---------------------------------------------------------------------------
# EntraIDValidator.validate_token tests
# ---------------------------------------------------------------------------


class TestValidateToken:
    """Tests for EntraIDValidator.validate_token."""

    def test_validate_token_success(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A valid token with correct claims is decoded successfully."""
        private_key, public_pem = rsa_keypair
        token = _make_token(private_key)

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        payload = validator.validate_token(token)

        assert payload["sub"] == "user-123"
        assert payload["aud"] == CLIENT_ID
        assert payload["iss"] == ISSUER

    def test_validate_token_rejects_expired(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """An expired token raises InvalidTokenError."""
        private_key, public_pem = rsa_keypair
        token = _make_token(private_key, exp_offset=-3600)

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        with pytest.raises(pyjwt.InvalidTokenError):
            validator.validate_token(token)

    def test_validate_token_rejects_wrong_audience(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A token with the wrong audience raises InvalidTokenError."""
        private_key, public_pem = rsa_keypair
        token = _make_token(private_key, aud="wrong-audience")

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        with pytest.raises(pyjwt.InvalidTokenError):
            validator.validate_token(token)

    def test_validate_token_rejects_wrong_issuer(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A token with the wrong issuer raises InvalidTokenError."""
        private_key, public_pem = rsa_keypair
        token = _make_token(private_key, iss="https://evil.example.com/v2.0")

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        with pytest.raises(pyjwt.InvalidTokenError):
            validator.validate_token(token)

    def test_validate_token_rejects_missing_sub_claim(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A token missing the required 'sub' claim raises InvalidTokenError."""
        private_key, public_pem = rsa_keypair
        token = _make_token_without_sub(private_key)

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        with pytest.raises(pyjwt.InvalidTokenError):
            validator.validate_token(token)


# ---------------------------------------------------------------------------
# get_current_user FastAPI dependency tests
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Tests for the get_current_user FastAPI dependency."""

    def test_get_current_user_rejects_missing_header(self) -> None:
        """A request without Authorization header returns 401."""
        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        app = _create_test_app(validator)
        client = TestClient(app)

        response = client.get("/me")

        assert response.status_code == 401
        assert "Missing Authorization header" in response.json()["detail"]

    def test_get_current_user_rejects_invalid_token(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A request with an invalid Bearer token returns 401."""
        _private_key, public_pem = rsa_keypair

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        app = _create_test_app(validator)
        client = TestClient(app)

        response = client.get(
            "/me", headers={"Authorization": "Bearer bad.token.here"}
        )

        assert response.status_code == 401
        assert "Invalid token" in response.json()["detail"]

    def test_get_current_user_returns_claims_on_valid_token(
        self, rsa_keypair: tuple[rsa.RSAPrivateKey, bytes]
    ) -> None:
        """A valid Bearer token returns the decoded claims."""
        private_key, public_pem = rsa_keypair
        token = _make_token(private_key, sub="user-456")

        validator = EntraIDValidator(TENANT_ID, CLIENT_ID)
        validator._jwks_client = _mock_jwks_client(public_pem)

        app = _create_test_app(validator)
        client = TestClient(app)

        response = client.get(
            "/me", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["sub"] == "user-456"
        assert body["aud"] == CLIENT_ID
        assert body["iss"] == ISSUER
