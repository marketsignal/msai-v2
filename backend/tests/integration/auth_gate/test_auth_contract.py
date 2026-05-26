"""Gate 2 — backend auth middleware contract test.

Each case mints a synthetic JWT with one specific manipulation, then
asserts BOTH layers:
- Layer A: validator.validate_token raises the SPECIFIC PyJWT subclass
- Layer B: GET /api/v1/account/health returns the correct HTTP status

No network calls (autouse _block_network fixture); no unit-test
override (autouse _clear_unit_auth_override fixture).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jwt
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from msai.core.auth import EntraIDValidator

# pytest-asyncio runs in `asyncio_mode = "auto"` (pyproject.toml:127); async
# tests are auto-marked. The sync `validate_token_or_api_key` tests below
# must NOT carry the asyncio mark, so we don't set a module-level pytestmark.


# ---------------------------------------------------------------------------
# Smoke test — FIRST in module so a missing override-clearance fails loudly.
# ---------------------------------------------------------------------------


async def test_unit_autouse_override_is_cleared(client: httpx.AsyncClient) -> None:
    """If the parent autouse override of get_current_user leaks into this
    module, every other test silently passes because the real validator
    never fires. This test asserts the override is cleared.
    """
    async with client as ac:
        resp = await ac.get("/api/v1/account/health")
    # Without Authorization header AND without X-API-Key, the real
    # dependency raises 401 "Missing Authorization header or X-API-Key".
    assert resp.status_code == 401
    assert "Missing Authorization" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Parametrised contract cases.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthCase:
    id: str
    overrides: dict[str, Any] = field(default_factory=dict)
    expected_exception: type[BaseException] | None = None
    expected_status: int = 401
    detail_substring: str | None = None


AUTH_CASES: list[AuthCase] = [
    AuthCase(id="positive", expected_exception=None, expected_status=200),
    AuthCase(
        id="graph_audience",
        overrides={"aud": "00000003-0000-0000-c000-000000000000"},
        expected_exception=jwt.InvalidAudienceError,
        detail_substring="Invalid token",
    ),
    AuthCase(
        id="wrong_tenant_in_issuer",
        overrides={
            "iss": ("https://login.microsoftonline.com/ffffffff-ffff-ffff-ffff-ffffffffffff/v2.0")
        },
        expected_exception=jwt.InvalidIssuerError,
    ),
    AuthCase(
        id="missing_scp",
        overrides={"scp": None},
        expected_exception=jwt.MissingRequiredClaimError,
    ),
    AuthCase(
        id="wrong_scp_user_read",
        overrides={"scp": "User.Read"},
        expected_exception=jwt.InvalidTokenError,
        detail_substring="Required scope 'access_as_user' missing",
    ),
    AuthCase(
        id="expired",
        overrides={"exp_delta_seconds": -10},
        expected_exception=jwt.ExpiredSignatureError,
    ),
    AuthCase(
        id="bad_kid",
        overrides={"kid": "unknown-kid", "sign_with_kid": "test-kid-current"},
        expected_exception=jwt.InvalidTokenError,
        detail_substring="Signing key not found",
    ),
    AuthCase(
        id="wrong_signature",
        overrides={"kid": "test-kid-current", "sign_with_kid": "test-kid-rotated"},
        expected_exception=jwt.InvalidSignatureError,
    ),
    AuthCase(
        id="app_only_roles_no_scp",
        overrides={"roles": ["api.access"], "scp": None},
        expected_exception=jwt.MissingRequiredClaimError,
    ),
    AuthCase(
        id="v1_issuer_host",
        overrides={"iss": ("https://sts.windows.net/00000000-0000-0000-0000-000000000000/")},
        expected_exception=jwt.InvalidIssuerError,
    ),
    AuthCase(
        id="ver_1_0",
        overrides={"ver": "1.0"},
        expected_exception=jwt.InvalidTokenError,
        detail_substring="Token version mismatch",
    ),
]


@pytest.mark.parametrize("case", AUTH_CASES, ids=lambda c: c.id)
async def test_auth_contract(
    client: httpx.AsyncClient,
    validator: EntraIDValidator,
    make_token: Callable[..., str],
    identity_contract: dict[str, Any],
    case: AuthCase,
) -> None:
    token = make_token(**case.overrides)

    # Layer A — validator raises the specific exception subclass
    if case.expected_exception is not None:
        with pytest.raises(case.expected_exception):
            validator.validate_token(token)
    else:
        # Positive — must NOT raise. Compare against contract values
        # (NOT hardcoded) so a contract/backend drift surfaces here.
        payload = validator.validate_token(token)
        assert payload["scp"] == identity_contract["scope_name"]
        assert payload["ver"] == identity_contract["token_version"]

    # Layer B — FastAPI surfaces the right HTTP status
    async with client as ac:
        resp = await ac.get(
            "/api/v1/account/health",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == case.expected_status, (
        f"case={case.id}: expected {case.expected_status}, got {resp.status_code}: {resp.text}"
    )
    if case.detail_substring is not None and case.expected_status == 401:
        detail = resp.json()["detail"]
        assert case.detail_substring in detail, (
            f"case={case.id}: expected detail substring {case.detail_substring!r} in {detail!r}"
        )


# ---------------------------------------------------------------------------
# Coverage assertion — second authenticated router (PRD US-001 AC).
# ---------------------------------------------------------------------------


async def test_auth_dependency_covers_other_router(
    client: httpx.AsyncClient,
) -> None:
    """Confirm an authenticated endpoint OUTSIDE the account router also
    enforces auth — catches the 'new router shipped without
    Depends(get_current_user)' regression class.

    Picks /api/v1/system/health (different router). We only assert the
    401-without-auth path because /system/health performs DB + Redis
    probes in its handler — we don't want to mock those just to assert
    the auth-wiring regression. Auth runs BEFORE the handler body, so
    missing-dependency surfaces as a non-401 status here.
    """
    async with client as ac:
        unauth = await ac.get("/api/v1/system/health")
    assert unauth.status_code == 401, (
        f"Auth dependency not wired on /api/v1/system/health: "
        f"expected 401, got {unauth.status_code}"
    )


# ---------------------------------------------------------------------------
# Task 9 — direct test for validate_token_or_api_key (WS path helper).
# Existing WS tests monkeypatch this helper rather than exercise it, so a
# JWT-shape regression here can ship undetected. Codex flagged in
# Contrarian gate + plan-review iter 1.
# ---------------------------------------------------------------------------


def test_validate_token_or_api_key_accepts_valid_jwt(
    validator: EntraIDValidator,  # noqa: ARG001 — installs validator at module scope
    make_token: Callable[..., str],
    identity_contract: dict[str, Any],
) -> None:
    """The WS helper path must accept a canonical delegated token."""
    from msai.core.auth import validate_token_or_api_key

    token = make_token()
    payload = validate_token_or_api_key(token)
    assert payload["scp"] == identity_contract["scope_name"]
    assert payload["ver"] == identity_contract["token_version"]


def test_validate_token_or_api_key_rejects_missing_scp(
    validator: EntraIDValidator,  # noqa: ARG001 — installs validator at module scope
    make_token: Callable[..., str],
) -> None:
    """The WS helper path must reject a JWT missing scp (Task 2 extension)."""
    from msai.core.auth import validate_token_or_api_key

    token = make_token(scp=None)
    with pytest.raises(jwt.MissingRequiredClaimError):
        validate_token_or_api_key(token)


def test_validate_token_or_api_key_accepts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API-key path is independent of the JWT path — must continue to work."""
    from msai.core import auth as auth_module
    from msai.core.auth import validate_token_or_api_key

    monkeypatch.setattr(auth_module.settings, "msai_api_key", "test-api-key-value")
    payload = validate_token_or_api_key("test-api-key-value")
    assert payload["sub"] == "api-key-user"
