"""Shared constructor for :class:`BrokerAccountService`.

Both the broker-account CRUD router (``api/broker_accounts``) and the
deploy-time credential validation in ``api/live`` need to build a
:class:`BrokerAccountService` with IDENTICAL store + backend selection
(security-relevant: Azure Key Vault in production, env-backed store in dev).
This module is the single source of truth so the two callsites can never
drift. Kept in ``api/`` (not the service layer) because it depends on the
FastAPI :class:`Request` to reach ``app.state.broker_credentials_store`` and
the service layer must not import FastAPI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from msai.core.config import settings
from msai.models.broker_account import CredentialsBackend
from msai.services.live.broker_account_service import BrokerAccountService

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession


def build_broker_account_service(request: Request, db: AsyncSession) -> BrokerAccountService:
    """Construct a :class:`BrokerAccountService` from app state + settings.

    The credentials store is wired onto ``app.state`` during the application
    lifespan; the gateway-slot pool and the credentials backend come from
    configuration (``env`` in dev, ``azure_kv`` in prod).
    """
    store = request.app.state.broker_credentials_store
    backend = (
        CredentialsBackend.AZURE_KV
        if settings.environment == "production"
        else CredentialsBackend.ENV
    )
    return BrokerAccountService(
        db,
        store=store,
        slots=settings.broker_gateway_slots,
        backend=backend,
    )
