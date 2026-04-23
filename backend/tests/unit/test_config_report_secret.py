"""Production-environment guard for the report-signing HMAC secret.

HMAC with an empty / weak key still produces verifiable tokens, so a
misconfigured deploy silently turns the signed-URL machinery into a
constant no-op (every ``/report?token=<x>`` verifies). The guard in
``msai.core.config.Settings._enforce_production_secrets`` must catch:

* the literal dev default string
* empty string
* anything shorter than 32 characters

and allow dev/test environments to keep their defaults (the developer
ergonomic concern).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from msai.core.config import Settings


def _base_env() -> dict[str, str]:
    """Minimum env vars needed to instantiate Settings successfully.

    Mirrors the fields without defaults so we can vary only the
    security-sensitive ones in each test.
    """
    return {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379",
        "data_root": "/tmp/msai-test",
        "azure_tenant_id": "tenant",
        "azure_client_id": "client",
        "jwt_tenant_id": "tenant",
        "jwt_client_id": "client",
    }


def test_production_rejects_default_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REPORT_SIGNING_SECRET", "dev-report-signing-secret-change-in-prod")

    with pytest.raises(ValidationError, match="non-default value in production"):
        Settings()


def test_production_rejects_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("REPORT_SIGNING_SECRET", "")

    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings()


def test_production_rejects_short_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # 31 chars — one below the floor.
    monkeypatch.setenv("REPORT_SIGNING_SECRET", "a" * 31)

    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings()


def test_production_accepts_strong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # 64 chars — representative of `openssl rand -base64 48` output.
    monkeypatch.setenv("REPORT_SIGNING_SECRET", "A" * 64)

    settings = Settings()
    assert settings.report_signing_secret == "A" * 64


def test_development_allows_default_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Developer ergonomics — dev default must keep working locally."""
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "development")
    # Don't set REPORT_SIGNING_SECRET — use the field default.

    settings = Settings()
    assert settings.report_signing_secret.startswith("dev-report-signing-secret")


def test_development_allows_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev/test environments intentionally skip the strong-secret floor so
    ephemeral CI jobs and docker-compose setups don't have to mint one.
    """
    for key, val in _base_env().items():
        monkeypatch.setenv(key.upper(), val)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("REPORT_SIGNING_SECRET", "")

    settings = Settings()
    assert settings.report_signing_secret == ""
