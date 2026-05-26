"""Shared fixtures for the auth-regression-gate integration tests.

Lives in its OWN subdir (``tests/integration/auth_gate/``) so the autouse
fixtures here do NOT pollute the parent ``tests/integration/`` suite,
which re-exports portfolio-backtest fixtures that conflict with our
network-block + auth-override-clearance pattern.

Three things this conftest provides that nothing else in the test tree does:

1. ``_block_network`` (autouse) — fail any test that attempts a real network
   call. JWKS lookups go through the mocked client; live Entra is never hit.
2. ``_clear_unit_auth_override`` (autouse, depends on parent ``_override_auth``)
   — pops the parent's ``app.dependency_overrides[get_current_user]`` AFTER
   it's been installed for the test, so the REAL dependency fires.
3. ``validator`` (module-scoped) — a real ``EntraIDValidator`` with its
   ``PyJWKClient`` swapped out for a deterministic mock that resolves
   keys by ``kid`` without network access.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from msai.core.auth import EntraIDValidator, get_current_user
from msai.main import app

# ---------------------------------------------------------------------------
# 1) Identity contract — single source of truth for token claims.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def identity_contract() -> dict[str, Any]:
    """Load the committed identity-contract.json (relative to repo root).

    conftest.py lives at backend/tests/integration/auth_gate/conftest.py
    → repo root is parents[4]. The parents[4] chain:
      parents[0] = .../auth_gate
      parents[1] = .../integration
      parents[2] = .../tests
      parents[3] = .../backend
      parents[4] = repo root (where identity-contract.json lives)
    """
    repo_root = Path(__file__).resolve().parents[4]
    contract_path = repo_root / "identity-contract.json"
    with contract_path.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 2) Network block — assert no live calls during the gate.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that attempts a real network call.

    The gate must run offline. JWKS lookups go through the patched mock
    client injected on the validator instance — not Microsoft's
    discovery URL.
    """

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "Network call blocked in integration auth-gate test. "
            "Use the mocked JWKS client; do not hit live Entra."
        )

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    monkeypatch.setattr(httpx, "get", _raise)
    # ASGITransport routes to the in-process app and does NOT call .send on
    # a real transport, so patching only the sync paths is sufficient to
    # block external libraries (PyJWKClient.fetch_jwks via urllib).


# ---------------------------------------------------------------------------
# 3) Clear the unit-test autouse override so the REAL dependency runs.
#
# Parent ``backend/tests/conftest.py:_override_auth`` is function-scoped
# autouse and re-installs ``app.dependency_overrides[get_current_user]``
# per test. A module-scoped pop would run once before the parent has a
# chance to re-install — the override would still leak. Function-scoped
# with explicit dependency on ``_override_auth`` lets pytest run the
# parent first, then this fixture pops the dict entry.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_unit_auth_override(_override_auth: Any) -> Generator[None, None, None]:
    """Pop the parent's get_current_user override AFTER it's been installed."""
    app.dependency_overrides.pop(get_current_user, None)
    yield


# ---------------------------------------------------------------------------
# 4) Test JWKS — two RSA keypairs (current key + rotated-out key).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def jwks_keypairs() -> dict[str, dict[str, Any]]:
    """Generate two RSA-2048 keypairs (current + rotated).

    Session-scoped to amortise the ~50ms RSA keygen cost across all tests.
    """
    keypairs: dict[str, dict[str, Any]] = {}
    for kid in ("test-kid-current", "test-kid-rotated"):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        keypairs[kid] = {
            "private_pem": private_pem,
            "public_pem": public_pem,
            "private_key": private_key,
        }
    return keypairs


# ---------------------------------------------------------------------------
# 5) Mock JWKS client — returns signing keys by kid; raises on unknown kid.
# ---------------------------------------------------------------------------


class _MockSigningKey:
    """Stand-in for jwt.PyJWK with the .key attribute PyJWT consumes."""

    def __init__(self, public_pem: bytes) -> None:
        self.key = public_pem


class _MockJWKSClient:
    """Mock PyJWKClient that resolves test JWKS without network access."""

    def __init__(self, keypairs: dict[str, dict[str, Any]]) -> None:
        self._keys = {kid: _MockSigningKey(kp["public_pem"]) for kid, kp in keypairs.items()}

    def get_signing_key_from_jwt(self, token: str) -> _MockSigningKey:
        # Decode header WITHOUT signature verification to extract kid.
        header = jwt.get_unverified_header(token)
        kid = header.get("kid", "")
        if kid not in self._keys:
            raise jwt.PyJWKClientError(f"Unable to find a signing key matching kid={kid!r}")
        return self._keys[kid]


@pytest.fixture(scope="session")
def mock_jwks_client(
    jwks_keypairs: dict[str, dict[str, Any]],
) -> _MockJWKSClient:
    return _MockJWKSClient(jwks_keypairs)


# ---------------------------------------------------------------------------
# 6) Real EntraIDValidator with the JWKS client swapped out.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def validator(
    identity_contract: dict[str, Any],
    mock_jwks_client: _MockJWKSClient,
) -> EntraIDValidator:
    """Construct a real EntraIDValidator, then replace its JWKS client.

    The constructor builds a real PyJWKClient (which does NOT fetch on
    construction), but we replace it immediately so no test can hit live
    Entra.
    """
    v = EntraIDValidator(
        tenant_id=identity_contract["tenant_id"],
        client_id=identity_contract["client_id"],
    )
    v._jwks_client = mock_jwks_client  # type: ignore[assignment]
    return v


@pytest.fixture(scope="module", autouse=True)
def _init_module_validator(
    validator: EntraIDValidator,
) -> Generator[None, None, None]:
    """Install the module's validator into the auth module's global slot
    so get_current_user finds it via get_validator()."""
    from msai.core import auth as auth_module

    prior = auth_module._validator
    auth_module._validator = validator
    try:
        yield
    finally:
        auth_module._validator = prior


# ---------------------------------------------------------------------------
# 7) Token factory — mints valid + invalid tokens for parametrised cases.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def make_token(
    identity_contract: dict[str, Any],
    jwks_keypairs: dict[str, dict[str, Any]],
):
    """Return a token-factory that produces signed JWTs for parametrised cases.

    Returns ``make_token(**overrides)`` — every claim has a sensible canonical
    delegated-user default; pass overrides to test specific negative cases.
    Defaults for ``scp`` and ``ver`` come from identity-contract.json (NOT
    hardcoded) so a contract value swap doesn't silently desync the test.
    """
    default_scp: str = identity_contract["scope_name"]
    default_ver: str = identity_contract["token_version"]

    def _factory(
        *,
        aud: str | None = ...,  # type: ignore[assignment]
        iss: str | None = ...,  # type: ignore[assignment]
        scp: str | None = ...,  # type: ignore[assignment]
        ver: str | None = ...,  # type: ignore[assignment]
        exp_delta_seconds: int = 3600,
        sub: str = "00000000-0000-0000-0000-000000000001",
        roles: list[str] | None = None,
        azp: str | None = ...,  # type: ignore[assignment]
        kid: str = "test-kid-current",
        sign_with_kid: str = "test-kid-current",
    ) -> str:
        if aud is ...:
            aud = identity_contract["client_id"]
        if iss is ...:
            iss = identity_contract["issuer"]
        if scp is ...:
            scp = default_scp
        if ver is ...:
            ver = default_ver
        if azp is ...:
            azp = identity_contract["client_id"]

        now = int(time.time())
        payload: dict[str, Any] = {
            "aud": aud,
            "iss": iss,
            "iat": now,
            "nbf": now,
            "exp": now + exp_delta_seconds,
            "sub": sub,
            "azp": azp,
            "azpacr": "0",
        }
        if scp is not None:
            payload["scp"] = scp
        if ver is not None:
            payload["ver"] = ver
        if roles is not None:
            payload["roles"] = roles

        sign_key = jwks_keypairs[sign_with_kid]["private_pem"]
        return jwt.encode(payload, sign_key, algorithm="RS256", headers={"kid": kid})

    return _factory


# ---------------------------------------------------------------------------
# 8) Async HTTP client through ASGITransport — hits the real FastAPI app.
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(_init_module_validator: None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")
