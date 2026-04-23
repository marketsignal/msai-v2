"""HMAC signer + verifier for short-lived report URLs.

Pattern adopted from S3 pre-signed URLs / Azure SAS / Cloudflare signed tokens.
Stateless: the backend mints a token that carries its own scope (backtest_id,
user_sub) and expiry; verification is a pure-function HMAC check. No session
store, no cookies, no cross-service SSO.

Token format: ``<base64url(payload_json)>.<hmac_sha256_hex>``

Intentionally NOT using JWT because we don't need the full JWS/JWT claim-set
machinery and JWT libraries add a large dependency surface. 40 lines of HMAC
is sufficient.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


class InvalidReportTokenError(ValueError):
    """Raised when a token is expired, tampered, malformed, or signed with a different secret."""


@dataclass(frozen=True, slots=True)
class ReportTokenClaims:
    backtest_id: UUID
    user_sub: str
    expires_at: datetime


def sign_report_token(
    *,
    backtest_id: UUID,
    user_sub: str,
    expires_at: datetime,
    secret: str,
) -> str:
    """Mint a signed, short-lived token that authenticates a report URL.

    The returned token is a two-part dotted string: ``<payload_b64>.<sig_hex>``.
    ``payload_b64`` encodes a sorted-keys JSON with ``backtest_id``, ``user_sub``,
    and integer ``exp`` (unix seconds UTC). ``sig_hex`` is the HMAC-SHA256 of the
    ``payload_b64`` string under ``secret``.
    """
    payload = {
        "backtest_id": str(backtest_id),
        "user_sub": user_sub,
        "exp": int(expires_at.timestamp()),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
    sig = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_report_token(
    token: str,
    *,
    backtest_id: UUID,
    secret: str,
) -> ReportTokenClaims:
    """Validate signature, expiry, and backtest_id match. Return claims or raise.

    Raises :class:`InvalidReportTokenError` on any failure path:
    malformed input, signature mismatch, expired token, or a token minted
    for a different ``backtest_id``.
    """
    # Cap the input size before any decoding work. A well-formed token is
    # well under 512 bytes; a caller passing gigabytes forces the base64
    # decoder to allocate proportionally. 4 KB is comfortable headroom.
    if len(token) > 4096:
        raise InvalidReportTokenError("oversized token")
    try:
        payload_b64, sig_hex = token.split(".", 1)
    except ValueError as e:
        raise InvalidReportTokenError("malformed token") from e

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_hex):
        raise InvalidReportTokenError("invalid signature")

    try:
        # Re-pad for base64 decode — urlsafe_b64encode strips ``=``, so
        # we need to add it back before decode.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload: dict[str, Any] = json.loads(payload_bytes)
    except (
        binascii.Error,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as e:
        # Narrow catch: only the known decode failure modes fold into
        # InvalidReportTokenError. A future TypeError or unexpected
        # exception from a library change surfaces instead of being
        # silently remapped to "malformed payload".
        raise InvalidReportTokenError("malformed payload") from e

    now = datetime.now(UTC)
    try:
        exp = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        token_backtest_id = UUID(payload["backtest_id"])
        user_sub = str(payload["user_sub"])
    except (KeyError, ValueError, TypeError) as e:
        raise InvalidReportTokenError("missing or invalid claim") from e

    if now >= exp:
        raise InvalidReportTokenError("token expired")
    if token_backtest_id != backtest_id:
        raise InvalidReportTokenError("backtest_id mismatch")

    return ReportTokenClaims(
        backtest_id=token_backtest_id,
        user_sub=user_sub,
        expires_at=exp,
    )
