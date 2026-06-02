"""Broker-account CRUD API router (``/api/v1/broker-accounts``).

Delegates to :class:`BrokerAccountService` (``msai.services.live``) for all
business logic. The router only translates HTTP verbs into service calls and
maps domain errors to the appropriate HTTP status codes.

Credentials-handling invariant (council Option B'): TWS credentials submitted
on create/rotate are written server-side to the credentials store and are
NEVER echoed back in any response — :class:`BrokerAccountResponse` carries
only credential references + audit columns. The router builds the service from
``request.app.state.broker_credentials_store`` (Azure Key Vault in prod, a
file-backed env store in dev), ``settings.broker_gateway_slots``, and the
environment-appropriate :class:`CredentialsBackend`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID  # noqa: TC003 -- FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.live.broker_credentials_store import BrokerCredentialsStore

from msai.core.auth import get_current_user, resolve_user_id
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.broker_account import CredentialsBackend
from msai.schemas.broker_account import (
    BrokerAccountCreateRequest,
    BrokerAccountResponse,
    BrokerAccountRotateCredentialsRequest,
    BrokerAccountUpdateRequest,
)
from msai.services.live.broker_account_service import (
    AccountArchivedError,
    AccountInUseError,
    AccountNotFoundError,
    BrokerAccountError,
    BrokerAccountService,
    DuplicateAccountError,
    NoFreeSlotError,
)
from msai.services.live.broker_credentials_store import (
    CredentialResolutionError,
    Credentials,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/broker-accounts", tags=["broker-accounts"])


def _build_service(request: Request, db: AsyncSession) -> BrokerAccountService:
    """Construct a :class:`BrokerAccountService` from app state + settings.

    The credentials store is wired onto ``app.state`` during the application
    lifespan; the gateway-slot pool and the credentials backend come from
    configuration (``env`` in dev, ``azure_kv`` in prod).
    """
    store: BrokerCredentialsStore = request.app.state.broker_credentials_store
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


def _actor(claims: dict[str, Any]) -> str:
    """Best-effort human-readable actor string for audit columns."""
    return str(claims.get("preferred_username") or claims.get("sub") or "unknown")


def _raise_for_service_error(exc: BrokerAccountError) -> NoReturn:
    """Map a :class:`BrokerAccountError` subclass to an HTTPException.

    Specific subclasses are checked BEFORE the base class (they all subclass
    :class:`BrokerAccountError`), so a slot/duplicate/in-use conflict never
    falls through to the catch-all 422.
    """
    if isinstance(exc, NoFreeSlotError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, DuplicateAccountError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, AccountInUseError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, AccountArchivedError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, AccountNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # Base catch-all (e.g. unknown / occupied pinned gateway_slot) → 422.
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /api/v1/broker-accounts -- register a new broker account
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=BrokerAccountResponse)
async def create_broker_account(
    body: BrokerAccountCreateRequest,
    request: Request,
    response: Response,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BrokerAccountResponse:
    """Register a broker account; the secret is written server-side first."""
    created_by = await resolve_user_id(db, claims)  # ensure the actor's user row exists
    svc = _build_service(request, db)
    actor = _actor(claims)
    try:
        acct = await svc.create(
            ib_account_id=body.ib_account_id,
            ib_login_key=body.ib_login_key,
            trading_mode=body.trading_mode,
            gateway_slot=body.gateway_slot,
            creds=Credentials(
                tws_userid=body.tws_userid.get_secret_value(),
                tws_password=body.tws_password.get_secret_value(),
            ),
            actor=actor,
            label=body.label,
            created_by=created_by,
        )
    except CredentialResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc.reason)
        ) from exc
    except BrokerAccountError as exc:
        _raise_for_service_error(exc)

    response.headers["Location"] = f"/api/v1/broker-accounts/{acct.id}"
    log.info(
        "broker_account_created",
        account_id=str(acct.id),
        ib_account_id=acct.ib_account_id,
        gateway_slot=acct.gateway_slot,
    )
    return BrokerAccountResponse.model_validate(acct)


# ---------------------------------------------------------------------------
# GET /api/v1/broker-accounts -- list active broker accounts
# ---------------------------------------------------------------------------


@router.get("", response_model=list[BrokerAccountResponse])
async def list_broker_accounts(
    request: Request,
    include_archived: bool = False,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[BrokerAccountResponse]:
    """List broker accounts (newest first). Excludes archived by default; pass
    ``?include_archived=true`` to also return archived audit rows so an archived
    account stays discoverable when explicitly requested."""
    svc = _build_service(request, db)
    accts = await svc.list(include_archived=include_archived)
    return [BrokerAccountResponse.model_validate(a) for a in accts]


# ---------------------------------------------------------------------------
# GET /api/v1/broker-accounts/{account_id} -- detail
# ---------------------------------------------------------------------------


@router.get("/{account_id}", response_model=BrokerAccountResponse)
async def get_broker_account(
    account_id: UUID,
    request: Request,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BrokerAccountResponse:
    """Get a single broker account by id."""
    svc = _build_service(request, db)
    try:
        acct = await svc.get(account_id)
    except BrokerAccountError as exc:
        _raise_for_service_error(exc)
    return BrokerAccountResponse.model_validate(acct)


# ---------------------------------------------------------------------------
# PATCH /api/v1/broker-accounts/{account_id} -- label / trading_mode only
# ---------------------------------------------------------------------------


@router.patch("/{account_id}", response_model=BrokerAccountResponse)
async def update_broker_account(
    account_id: UUID,
    body: BrokerAccountUpdateRequest,
    request: Request,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BrokerAccountResponse:
    """Update mutable fields (``label`` / ``trading_mode``) only.

    Distinguishes 'field omitted' from an explicit ``null`` via Pydantic v2
    ``model_fields_set``: a key the caller actually sent is forwarded (so PATCH
    ``{"label": null}`` clears the label), an omitted key is left as the
    service's :data:`UNSET` sentinel so the stored value is preserved.
    """
    svc = _build_service(request, db)
    sent = body.model_fields_set
    update_kwargs: dict[str, Any] = {}
    if "label" in sent:
        update_kwargs["label"] = body.label
    if "trading_mode" in sent:
        update_kwargs["trading_mode"] = body.trading_mode
    try:
        acct = await svc.update(account_id, **update_kwargs)
    except BrokerAccountError as exc:
        _raise_for_service_error(exc)

    log.info("broker_account_updated", account_id=str(account_id))
    return BrokerAccountResponse.model_validate(acct)


# ---------------------------------------------------------------------------
# POST /api/v1/broker-accounts/{account_id}/rotate-credentials
# ---------------------------------------------------------------------------


@router.post("/{account_id}/rotate-credentials", response_model=BrokerAccountResponse)
async def rotate_broker_account_credentials(
    account_id: UUID,
    body: BrokerAccountRotateCredentialsRequest,
    request: Request,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BrokerAccountResponse:
    """Rotate the account's TWS credentials (store first, then stamp the row)."""
    svc = _build_service(request, db)
    actor = _actor(claims)
    try:
        acct = await svc.rotate(
            account_id,
            creds=Credentials(
                tws_userid=body.tws_userid.get_secret_value(),
                tws_password=body.tws_password.get_secret_value(),
            ),
            actor=actor,
        )
    except CredentialResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc.reason)
        ) from exc
    except BrokerAccountError as exc:
        _raise_for_service_error(exc)

    log.info("broker_account_credentials_rotated", account_id=str(account_id))
    return BrokerAccountResponse.model_validate(acct)


# ---------------------------------------------------------------------------
# POST /api/v1/broker-accounts/{account_id}/archive -- soft-delete
# ---------------------------------------------------------------------------


@router.post("/{account_id}/archive", response_model=BrokerAccountResponse)
async def archive_broker_account(
    account_id: UUID,
    request: Request,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BrokerAccountResponse:
    """Archive the account, freeing its gateway slot and deleting its secret.

    Blocks (409) if a deployment bound to this account is in an active
    lifecycle state. Modeled as POST (not DELETE) because it transitions the
    row to ``archived`` and returns the updated resource, rather than removing
    it — mirroring the service's soft-delete semantics.
    """
    svc = _build_service(request, db)
    actor = _actor(claims)
    try:
        acct = await svc.archive(account_id, actor=actor)
    except CredentialResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc.reason)
        ) from exc
    except BrokerAccountError as exc:
        _raise_for_service_error(exc)

    log.info("broker_account_archived", account_id=str(account_id))
    return BrokerAccountResponse.model_validate(acct)
