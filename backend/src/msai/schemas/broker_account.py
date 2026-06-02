"""Pydantic schemas for broker-account CRUD API endpoints.

These schemas map to the ``broker_accounts`` table introduced for the
multi-account IB broker fleet. They enforce the credentials-handling
invariant ratified by council (Option B'): TWS credentials are written
server-side to the secrets backend on create/rotate and are NEVER returned
in any response — the response carries only metadata references
(``credentials_secret_ref`` / ``credentials_secret_version``) and audit
columns.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves at runtime
from uuid import UUID  # noqa: TC003 — Pydantic resolves at runtime

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from msai.services.nautilus.ib_port_validator import assert_account_mode_consistent


class BrokerAccountCreateRequest(BaseModel):
    """POST body for registering a new broker account.

    Carries the TWS credentials inbound so the backend can write them to the
    secrets backend; they are never echoed back in any response.
    """

    ib_account_id: str = Field(max_length=32, pattern=r"^[A-Za-z0-9]+$")
    # min_length=1 + a strip/non-blank validator mirror the live.py
    # LiveStartPortfolioRequest.ib_login_key boundary: a blank or
    # whitespace-only login key is the routing key the GatewayRouter resolves,
    # so it must never be empty.
    ib_login_key: str = Field(min_length=1, max_length=64)
    label: str | None = None
    trading_mode: str = Field(default="paper", pattern=r"^(paper|live)$")
    gateway_slot: str | None = None  # None → auto-allocate a free slot
    # SecretStr so a rejected value (e.g. too-long password) is masked in
    # FastAPI's 422 ``input`` echo. Field min/max_length validate the wrapped
    # secret in Pydantic v2.
    tws_userid: SecretStr = Field(min_length=1, max_length=256)
    tws_password: SecretStr = Field(min_length=1, max_length=512)

    @field_validator("ib_login_key")
    @classmethod
    def _non_blank_login_key(cls, v: str) -> str:
        """Strip and reject a whitespace-only ``ib_login_key`` (422).

        The login key is the GatewayRouter routing key; a blank value would
        resolve to no gateway. Matches the LiveStartPortfolioRequest pattern.
        """
        normalized = v.strip()
        if not normalized:
            raise ValueError("ib_login_key cannot be empty / whitespace-only")
        return normalized

    @model_validator(mode="after")
    def _account_prefix_matches_trading_mode(self) -> BrokerAccountCreateRequest:
        """Reject an IB account-id / trading_mode mismatch (422).

        Delegates to the single shared guard
        (``ib_port_validator.assert_account_mode_consistent``) so the create-time
        and update-time checks can never drift: paper accounts are ``DU``/``DF``
        prefixed; live accounts start with ``U`` and are NOT ``DU``/``DF``.
        Pairing a live account with ``paper`` mode (or a paper account with
        ``live`` mode) silently misroutes orders at the gateway (nautilus gotcha
        #6) — caught here at the broker-account boundary so the two control
        planes agree.
        """
        assert_account_mode_consistent(self.ib_account_id, self.trading_mode)
        return self


class BrokerAccountUpdateRequest(BaseModel):
    """PATCH body for a broker account — label and trading_mode only."""

    label: str | None = None
    trading_mode: str | None = Field(default=None, pattern=r"^(paper|live)$")
    # ib_account_id is IMMUTABLE — intentionally absent.


class BrokerAccountRotateCredentialsRequest(BaseModel):
    """POST body for rotating a broker account's TWS credentials."""

    # SecretStr masks a rejected value in the 422 echo (see create request).
    tws_userid: SecretStr = Field(min_length=1, max_length=256)
    tws_password: SecretStr = Field(min_length=1, max_length=512)


class BrokerAccountResponse(BaseModel):
    """Response schema for a broker account — metadata only, NO secrets.

    Critically excludes ``tws_userid`` / ``tws_password``; only credential
    references and audit columns are exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ib_account_id: str
    ib_login_key: str
    label: str | None
    status: str
    gateway_slot: str
    trading_mode: str
    credentials_backend: str
    credentials_secret_ref: str
    credentials_secret_version: str | None
    credentials_updated_at: datetime | None
    credentials_updated_by: str | None
    credentials_last_accessed: datetime | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
