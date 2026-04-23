"""Unit tests for the report-signer HMAC helpers.

Five cases (per plan spec):

1. sign → verify roundtrip succeeds and returns the minted claims.
2. Expired token → ``InvalidReportTokenError`` with "expired" in the message.
3. Tampered payload / cross-backtest-id → rejected.
4. Wrong-secret verify → signature mismatch rejection.
5. Garbage input → any ``InvalidReportTokenError`` (malformed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from msai.services.report_signer import (
    InvalidReportTokenError,
    sign_report_token,
    verify_report_token,
)


def test_sign_then_verify_roundtrip() -> None:
    backtest_id = uuid4()
    user_sub = "test-user"
    expires_at = datetime.now(UTC) + timedelta(seconds=60)
    token = sign_report_token(
        backtest_id=backtest_id,
        user_sub=user_sub,
        expires_at=expires_at,
        secret="test-secret",
    )
    # Must roundtrip cleanly
    claims = verify_report_token(token, backtest_id=backtest_id, secret="test-secret")
    assert claims.backtest_id == backtest_id
    assert claims.user_sub == user_sub


def test_verify_rejects_expired_token() -> None:
    backtest_id = uuid4()
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    token = sign_report_token(
        backtest_id=backtest_id,
        user_sub="u",
        expires_at=expired_at,
        secret="test-secret",
    )
    with pytest.raises(InvalidReportTokenError, match="expired"):
        verify_report_token(token, backtest_id=backtest_id, secret="test-secret")


def test_verify_rejects_tampered_payload() -> None:
    """A token minted for backtest A must not unlock backtest B."""
    bt_a, bt_b = uuid4(), uuid4()
    token = sign_report_token(
        backtest_id=bt_a,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="s",
    )
    with pytest.raises(InvalidReportTokenError):
        verify_report_token(token, backtest_id=bt_b, secret="s")


def test_verify_rejects_wrong_secret() -> None:
    """A token signed with secret A must not validate under secret B."""
    bt = uuid4()
    token = sign_report_token(
        backtest_id=bt,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="secret-a",
    )
    with pytest.raises(InvalidReportTokenError, match="signature"):
        verify_report_token(token, backtest_id=bt, secret="secret-b")


def test_verify_rejects_garbage_token() -> None:
    with pytest.raises(InvalidReportTokenError):
        verify_report_token("not.a.token", backtest_id=uuid4(), secret="s")


def test_verify_rejects_zero_ttl_token_immediately_expired() -> None:
    """A token minted with expires_at==now is already expired by the time verify runs."""
    bt = uuid4()
    now = datetime.now(UTC)
    token = sign_report_token(
        backtest_id=bt,
        user_sub="u",
        expires_at=now,  # zero-TTL: already at/past expiry
        secret="s",
    )
    with pytest.raises(InvalidReportTokenError, match="expired"):
        verify_report_token(token, backtest_id=bt, secret="s")


def test_sign_with_empty_secret_produces_verifiable_token() -> None:
    """Empty secret is permitted by the HMAC layer (RFC 2104 allows empty keys),
    which is why the signer itself doesn't reject it — callers need a way to
    test signer semantics without a real secret.

    The production-secret guard in ``msai.core.config._enforce_production_secrets``
    refuses to instantiate ``Settings`` when ``environment=="production"`` AND
    the secret is empty / shorter than 32 chars. That's the deployment-time
    backstop; this test pins signer-only behavior.
    """
    bt = uuid4()
    token = sign_report_token(
        backtest_id=bt,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="",
    )
    claims = verify_report_token(token, backtest_id=bt, secret="")
    assert claims.backtest_id == bt


def test_verify_rejects_payload_with_stale_signature() -> None:
    """Classic MAC attack: decode the base64 payload, swap the backtest_id
    while keeping the original signature, re-encode, and try to verify. The
    HMAC-compare over the payload must detect the tampered payload and fail
    with "invalid signature" — NOT "backtest_id mismatch" (that would mean
    the signer's compare_digest was doing the work, not MAC verification).
    """
    import base64
    import json as _json
    from uuid import UUID

    original_bt = uuid4()
    other_bt = UUID("00000000-0000-0000-0000-000000000001")
    token = sign_report_token(
        backtest_id=original_bt,
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret="s",
    )

    # Split into the two halves the signer uses: payload_b64 + sig_hex.
    payload_b64, sig_hex = token.split(".", 1)

    # Decode, mutate backtest_id, re-encode — keep the original signature.
    padding = "=" * (-len(payload_b64) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    payload["backtest_id"] = str(other_bt)
    tampered_b64 = (
        base64.urlsafe_b64encode(_json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    )
    tampered_token = f"{tampered_b64}.{sig_hex}"

    # Try to verify against EITHER backtest id. Both must raise with
    # "invalid signature" — the MAC check runs before the id compare.
    with pytest.raises(InvalidReportTokenError, match="invalid signature"):
        verify_report_token(tampered_token, backtest_id=original_bt, secret="s")
    with pytest.raises(InvalidReportTokenError, match="invalid signature"):
        verify_report_token(tampered_token, backtest_id=other_bt, secret="s")


def test_verify_rejects_oversized_token() -> None:
    """Memory-DoS guard: a caller passing gigabytes must be cut off before
    base64 decode allocates proportionally.
    """
    oversized = "a" * 5000 + "." + "0" * 64
    with pytest.raises(InvalidReportTokenError, match="oversized token"):
        verify_report_token(oversized, backtest_id=uuid4(), secret="s")


def test_verify_rejects_non_ascii_payload_as_malformed() -> None:
    """Non-ASCII in the payload segment would crash `str.encode('ascii')` with
    UnicodeEncodeError, bubbling as a 500 instead of the intended 401. A
    well-formed base64url payload is ASCII-only by definition, so non-ASCII
    must fold into InvalidReportTokenError at the validation boundary.
    """
    non_ascii_token = "payload-é." + "0" * 64
    with pytest.raises(InvalidReportTokenError, match="malformed token"):
        verify_report_token(non_ascii_token, backtest_id=uuid4(), secret="s")
