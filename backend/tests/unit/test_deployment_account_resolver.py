"""Unit tests for the deployment account resolver + two-stage validation gate.

These cover :mod:`msai.services.live.deployment_account_resolver`:

* ``resolve_active_broker_account`` — maps a deploy request (by row UUID or by
  ``ib_account_id``) to exactly one ACTIVE :class:`BrokerAccount`.
* ``validate_account_row_state`` — STAGE 1, cheap row-state + route checks; no
  Key Vault read, no commit. Runs early (before idempotency hashing).
* ``validate_account_credentials`` — STAGE 2, side-effectful credential
  RESOLVABILITY check via ``BrokerAccountService.resolve_for_spawn``. Runs late
  (after the idempotency reservation) because ``resolve_for_spawn`` commits +
  stamps ``credentials_last_accessed`` + sets the KV-age gauge.

The DB session is faked (a hand-written stub that emulates ``AsyncSession.get``
by primary key and ``AsyncSession.execute`` by applying the compiled WHERE
params) — the resolver's branching logic is the unit under test, not SQLAlchemy.
``BrokerAccountService.resolve_for_spawn`` is mocked at the service boundary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from msai.models.broker_account import (
    BrokerAccount,
    BrokerAccountStatus,
    CredentialsBackend,
)
from msai.services.live.broker_account_service import (
    AccountArchivedError,
)
from msai.services.live.broker_credentials_store import (
    CredentialResolutionError,
    Credentials,
    KvFailureReason,
    ResolvedSpawnCredentials,
)
from msai.services.live.deployment_account_resolver import (
    AccountNotResolvable,
    DeployValidation,
    resolve_active_broker_account,
    validate_account_credentials,
    validate_account_row_state,
)
from msai.services.live.gateway_router import GatewayRouter
from msai.services.observability.broker_account_metrics import DEPLOY_VALIDATION_FAILED


def _deploy_validation_total() -> float:
    total = 0.0
    for line in DEPLOY_VALIDATION_FAILED.render():
        if line.startswith("msai_broker_account_deploy_validation_failed_total{"):
            total += float(line.rsplit(" ", 1)[1])
    return total


def _make_account(
    *,
    ib_account_id: str = "U4705114",
    ib_login_key: str = "mslvp",
    status: BrokerAccountStatus = BrokerAccountStatus.ACTIVE,
    trading_mode: str = "live",
    backend: CredentialsBackend = CredentialsBackend.AZURE_KV,
    secret_version: str | None = "v7",
    account_id: UUID | None = None,
) -> BrokerAccount:
    return BrokerAccount(
        id=account_id or uuid4(),
        ib_account_id=ib_account_id,
        ib_login_key=ib_login_key,
        status=status,
        gateway_slot="slot-a",
        trading_mode=trading_mode,
        credentials_backend=backend,
        credentials_secret_ref="broker-cred-x",
        credentials_secret_version=secret_version,
    )


class _FakeResult:
    def __init__(self, row: BrokerAccount | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> BrokerAccount | None:
        return self._row


class _FakeSession:
    """Minimal AsyncSession stand-in for the resolver's two query shapes.

    ``get(model, pk)`` matches a seeded row by primary key (no status filter —
    mirrors ``AsyncSession.get``). ``execute(stmt)`` emulates the
    ``ib_account_id == X AND status == ACTIVE`` SELECT by reading the compiled
    bind params off the statement and filtering the seeded rows.
    """

    def __init__(self, rows: list[BrokerAccount]) -> None:
        self._rows = rows

    async def get(self, model: type[BrokerAccount], pk: UUID) -> BrokerAccount | None:
        for row in self._rows:
            if row.id == pk:
                return row
        return None

    async def execute(self, stmt: Any) -> _FakeResult:
        params = stmt.compile().params
        wanted_ib = params.get("ib_account_id_1")
        wanted_status = params.get("status_1")
        for row in self._rows:
            if row.ib_account_id == wanted_ib and row.status == wanted_status:
                return _FakeResult(row)
        return _FakeResult(None)


# --------------------------------------------------------------------------- #
# resolve_active_broker_account
# --------------------------------------------------------------------------- #


async def test_resolve_by_broker_account_id_active_returns_it():
    # Arrange
    acct = _make_account()
    db = _FakeSession([acct])

    # Act
    result = await resolve_active_broker_account(db, broker_account_id=acct.id, ib_account_id=None)

    # Assert
    assert result is acct


async def test_resolve_by_ib_account_id_active_returns_it():
    # Arrange
    acct = _make_account(ib_account_id="U4705114")
    db = _FakeSession([acct])

    # Act
    result = await resolve_active_broker_account(
        db, broker_account_id=None, ib_account_id="U4705114"
    )

    # Assert
    assert result is acct


async def test_resolve_by_ib_account_id_archived_only_raises_not_resolvable():
    # Arrange — only an ARCHIVED row exists for this ib_account_id
    acct = _make_account(ib_account_id="U4705114", status=BrokerAccountStatus.ARCHIVED)
    db = _FakeSession([acct])

    # Act / Assert
    with pytest.raises(AccountNotResolvable):
        await resolve_active_broker_account(db, broker_account_id=None, ib_account_id="U4705114")


async def test_resolve_by_unknown_broker_account_id_raises_not_resolvable():
    # Arrange
    db = _FakeSession([])

    # Act / Assert
    with pytest.raises(AccountNotResolvable):
        await resolve_active_broker_account(db, broker_account_id=uuid4(), ib_account_id=None)


async def test_resolve_by_unknown_ib_account_id_raises_not_resolvable():
    # Arrange
    db = _FakeSession([])

    # Act / Assert
    with pytest.raises(AccountNotResolvable):
        await resolve_active_broker_account(db, broker_account_id=None, ib_account_id="U9999999")


# --------------------------------------------------------------------------- #
# validate_account_row_state (STAGE 1 — cheap, no KV, no commit)
# --------------------------------------------------------------------------- #


def _router(config: str | None) -> GatewayRouter:
    return GatewayRouter(config)


async def test_row_state_archived_account_is_invalid_archived():
    # Arrange
    acct = _make_account(status=BrokerAccountStatus.ARCHIVED)
    router = _router("mslvp:ib-gateway:4003:accounts=U4705114")

    # Act
    result = validate_account_row_state(acct, requested_mode="live", gateway_router=router)

    # Assert
    assert result == DeployValidation(valid=False, reason="archived")


async def test_row_state_mode_mismatch_is_invalid_mode_inconsistent():
    # Arrange — a live-prefix (U) account requested in paper mode
    acct = _make_account(ib_account_id="U4705114", trading_mode="live")
    router = _router("mslvp:ib-gateway:4003:accounts=U4705114")

    # Act
    result = validate_account_row_state(acct, requested_mode="paper", gateway_router=router)

    # Assert
    assert result.valid is False
    assert result.reason == "mode_inconsistent"


async def test_row_state_unknown_login_is_invalid_route_not_found():
    # Arrange — router has no entry for this login key
    acct = _make_account(ib_login_key="missing", ib_account_id="U4705114")
    router = _router("mslvp:ib-gateway:4003:accounts=U4705114")

    # Act
    result = validate_account_row_state(acct, requested_mode="live", gateway_router=router)

    # Assert
    assert result.valid is False
    assert result.reason == "route_not_found"


async def test_row_state_login_bound_but_account_not_in_binding_is_invalid_not_router_bound():
    # Arrange — login resolves, has a non-empty accounts binding, but THIS account is not in it
    acct = _make_account(ib_login_key="mslvp", ib_account_id="U4705114")
    router = _router("mslvp:ib-gateway:4003:accounts=U9999999")

    # Act
    result = validate_account_row_state(acct, requested_mode="live", gateway_router=router)

    # Assert
    assert result.valid is False
    assert result.reason == "not_router_bound"


async def test_row_state_all_good_is_valid_with_no_version():
    # Arrange
    acct = _make_account(ib_login_key="mslvp", ib_account_id="U4705114")
    router = _router("mslvp:ib-gateway:4003:accounts=U4705114")

    # Act
    result = validate_account_row_state(acct, requested_mode="live", gateway_router=router)

    # Assert — stage 1 never reads KV, so version is None even on success
    assert result == DeployValidation(valid=True, reason=None, version=None)


async def test_row_state_unbound_login_empty_accounts_is_valid():
    # Arrange — 3-tuple form: login resolves but has NO accounts binding (accounts_for == [])
    acct = _make_account(ib_login_key="mslvp", ib_account_id="U4705114")
    router = _router("mslvp:ib-gateway:4003")

    # Act
    result = validate_account_row_state(acct, requested_mode="live", gateway_router=router)

    # Assert — empty binding means "not enforced", so this is allowed
    assert result.valid is True


# --------------------------------------------------------------------------- #
# validate_account_credentials (STAGE 2 — KV, side-effectful)
# --------------------------------------------------------------------------- #


def _service_returning(creds: Credentials, version_used: str | None = None) -> MagicMock:
    # resolve_for_spawn now returns ResolvedSpawnCredentials carrying the version
    # it ACTUALLY read against — validate_account_credentials must surface THAT
    # version, never re-read the account ORM attribute.
    svc = MagicMock()
    svc.resolve_for_spawn = AsyncMock(
        return_value=ResolvedSpawnCredentials(creds=creds, version_used=version_used)
    )
    return svc


def _service_raising(exc: BaseException) -> MagicMock:
    svc = MagicMock()
    svc.resolve_for_spawn = AsyncMock(side_effect=exc)
    return svc


async def test_credentials_resolution_error_is_invalid_with_reason_no_secret_echoed():
    # Arrange
    acct = _make_account()
    svc = _service_raising(
        CredentialResolutionError(KvFailureReason.UNAUTHORIZED, "broker-cred-x", "denied")
    )
    before = _deploy_validation_total()

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — reason is the KvFailureReason value, never a secret
    assert result.valid is False
    assert result.reason == KvFailureReason.UNAUTHORIZED.value
    # Deploy-gate semantics: STAGE 2 calls resolve_for_spawn with
    # stamp_access=False (no commit, no last-accessed stamp, no lock held).
    svc.resolve_for_spawn.assert_awaited_once_with(acct.id, stamp_access=False)
    # A CredentialResolutionError is already counted by SPAWN_FAILED inside
    # resolve_for_spawn — it must NOT also increment DEPLOY_VALIDATION_FAILED
    # (no double-count).
    assert _deploy_validation_total() == before


async def test_credentials_archived_account_is_invalid_archived():
    # Arrange
    acct = _make_account(status=BrokerAccountStatus.ARCHIVED)
    svc = _service_raising(AccountArchivedError("archived"))
    before = _deploy_validation_total()

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert
    assert result.valid is False
    assert result.reason == "archived"
    # The defensive archived-at-stage-2 branch is NOT counted elsewhere, so it
    # increments DEPLOY_VALIDATION_FAILED so the rejection is observable.
    assert _deploy_validation_total() > before


async def test_credentials_aliased_login_key_with_different_real_userid_is_valid():
    # Arrange — ``ib_login_key`` is a routing ALIAS ("lvp"), while the resolved
    # secret's ``tws_userid`` is the REAL IB username ("marin1016"). These are
    # SUPPOSED to differ for every migrated account (the backfill seeds aliases
    # like lvp/hvp as the login key; the env secret returns the true username).
    # The old ``login_mismatch`` guard wrongly fail-closed this valid case; it
    # has been removed. This account MUST validate.
    acct = _make_account(
        ib_login_key="lvp",
        backend=CredentialsBackend.AZURE_KV,
        secret_version="v7",
    )
    svc = _service_returning(
        Credentials(tws_userid="marin1016", tws_password="pw"), version_used="v7"
    )
    before = _deploy_validation_total()

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — valid, pinned version surfaced, NO rejection counted
    assert result == DeployValidation(valid=True, reason=None, version="v7")
    # Deploy-gate semantics: stamp_access=False (no commit / no stamp / no lock).
    svc.resolve_for_spawn.assert_awaited_once_with(acct.id, stamp_access=False)
    assert _deploy_validation_total() == before


async def test_credentials_legacy_env_skips_resolve_for_spawn_is_valid_null_version():
    # Arrange — a legacy_env active row's credentials live ONLY in the IB Gateway
    # containers' env (TWS_USERID/TWS_PASSWORD); the BACKEND API process cannot
    # read them. So STAGE 2 MUST NOT attempt the env read for legacy_env — it
    # skips resolve_for_spawn entirely and validates row-state only (STAGE 1),
    # deferring credential authority to the gateway at spawn. version is NULL.
    acct = _make_account(
        ib_login_key="mslvp",
        backend=CredentialsBackend.LEGACY_ENV,
        secret_version=None,
    )
    svc = _service_returning(Credentials(tws_userid="mslvp", tws_password="pw"))
    before = _deploy_validation_total()

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — valid, NULL version, and the env read was NEVER attempted.
    assert result == DeployValidation(valid=True, reason=None, version=None)
    svc.resolve_for_spawn.assert_not_awaited()
    # No rejection counted on the skip path.
    assert _deploy_validation_total() == before


async def test_credentials_env_file_store_backend_still_calls_resolve_for_spawn():
    # Arrange — the dev ``env`` (file-backed) store IS readable by the backend
    # (mounted file), so STAGE 2 still performs the credential read for it. Only
    # ``legacy_env`` (gateway-env-var refs) is skipped.
    acct = _make_account(
        ib_login_key="mslvp",
        backend=CredentialsBackend.ENV,
        secret_version="v3",
    )
    svc = _service_returning(Credentials(tws_userid="mslvp", tws_password="pw"), version_used="v3")

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — read attempted, version surfaced.
    assert result == DeployValidation(valid=True, reason=None, version="v3")
    svc.resolve_for_spawn.assert_awaited_once_with(acct.id, stamp_access=False)


async def test_credentials_non_legacy_resolvable_is_valid_with_pinned_version():
    # Arrange — non-legacy active row reports its pinned secret version on success
    acct = _make_account(
        ib_login_key="mslvp",
        backend=CredentialsBackend.AZURE_KV,
        secret_version="v7",
    )
    svc = _service_returning(Credentials(tws_userid="mslvp", tws_password="pw"), version_used="v7")

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert
    assert result == DeployValidation(valid=True, reason=None, version="v7")
    svc.resolve_for_spawn.assert_awaited_once_with(acct.id, stamp_access=False)


async def test_credentials_version_comes_from_resolved_version_used_not_orm_attribute():
    # Regression (P2 credential-version-reporting race): if a credential ROTATION
    # commits a NEWER version onto the row WHILE resolve_for_spawn is executing,
    # the post-call ORM attribute reflects the new version (v2), but the store
    # read inside resolve_for_spawn used the version current AT READ TIME (v1).
    # validate_account_credentials MUST report the version the read ACTUALLY used
    # (v1, from ResolvedSpawnCredentials.version_used) — never the post-call ORM
    # attribute (v2). Otherwise the STAGE-3 rotation guard compares v2==v2 and is
    # defeated, stamping the deployment as validated against creds it never read.
    #
    # Simulate the race deterministically: the account row already shows the
    # ROTATED version v2, while resolve_for_spawn returns version_used="v1" (the
    # version it actually resolved before the rotation landed).
    acct = _make_account(
        ib_login_key="lvp",
        backend=CredentialsBackend.AZURE_KV,
        secret_version="v2",  # post-call ORM attribute (rotated) — must NOT be used
    )
    svc = _service_returning(
        Credentials(tws_userid="marin1016", tws_password="pw"), version_used="v1"
    )

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — reported version is the RESOLVED version (v1), not the ORM attr (v2)
    assert result == DeployValidation(valid=True, reason=None, version="v1")
    assert result.version != acct.credentials_secret_version


# --------------------------------------------------------------------------- #
# P2-A — transient vs permanent KvFailureReason classification.
#
# A transient infra failure (KV throttled/unreachable) is RETRYABLE — the
# handler maps it to 503 so operators retry rather than treating a deploy-time
# Key Vault outage as permanent invalid input. A permanent failure
# (unauthorized / not-found / decrypt-failed) maps to 422. The classification
# itself lives next to the enum (``is_transient_kv_reason``); the resolver
# surfaces it on ``DeployValidation.transient`` so the handler does not have to
# re-classify per call.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reason",
    [KvFailureReason.THROTTLED, KvFailureReason.UNREACHABLE],
)
def test_is_transient_kv_reason_true_for_transient(reason: KvFailureReason) -> None:
    from msai.services.live.broker_credentials_store import is_transient_kv_reason

    assert is_transient_kv_reason(reason) is True
    assert is_transient_kv_reason(reason.value) is True


@pytest.mark.parametrize(
    "reason",
    [
        KvFailureReason.UNAUTHORIZED,
        KvFailureReason.NOT_FOUND,
        KvFailureReason.DECRYPT_FAILED,
    ],
)
def test_is_transient_kv_reason_false_for_permanent(reason: KvFailureReason) -> None:
    from msai.services.live.broker_credentials_store import is_transient_kv_reason

    assert is_transient_kv_reason(reason) is False
    assert is_transient_kv_reason(reason.value) is False


def test_is_transient_kv_reason_false_for_non_kv_reason() -> None:
    # Row-state reasons ("archived", "route_not_found", ...) are never transient.
    from msai.services.live.broker_credentials_store import is_transient_kv_reason

    assert is_transient_kv_reason("archived") is False
    assert is_transient_kv_reason(None) is False


@pytest.mark.parametrize(
    "reason",
    [KvFailureReason.THROTTLED, KvFailureReason.UNREACHABLE],
)
async def test_credentials_transient_kv_failure_marks_transient(
    reason: KvFailureReason,
) -> None:
    # Arrange — resolve_for_spawn raises a TRANSIENT KvFailureReason.
    acct = _make_account()
    svc = _service_raising(CredentialResolutionError(reason, "broker-cred-x", "kv down"))

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — invalid, reason surfaced, AND flagged transient (→ 503 retryable).
    assert result.valid is False
    assert result.reason == reason.value
    assert result.transient is True


@pytest.mark.parametrize(
    "reason",
    [
        KvFailureReason.UNAUTHORIZED,
        KvFailureReason.NOT_FOUND,
        KvFailureReason.DECRYPT_FAILED,
    ],
)
async def test_credentials_permanent_kv_failure_not_transient(
    reason: KvFailureReason,
) -> None:
    # Arrange — resolve_for_spawn raises a PERMANENT KvFailureReason.
    acct = _make_account()
    svc = _service_raising(CredentialResolutionError(reason, "broker-cred-x", "bad"))

    # Act
    result = await validate_account_credentials(acct, svc)

    # Assert — invalid, NOT transient (→ 422 permanent invalid-input).
    assert result.valid is False
    assert result.reason == reason.value
    assert result.transient is False


async def test_credentials_archived_at_stage2_is_not_transient() -> None:
    # The defensive archived-between-stages branch is a PERMANENT row-state
    # rejection (422), never transient.
    acct = _make_account(status=BrokerAccountStatus.ARCHIVED)
    svc = _service_raising(AccountArchivedError("archived"))

    result = await validate_account_credentials(acct, svc)

    assert result.valid is False
    assert result.reason == "archived"
    assert result.transient is False


async def test_credentials_valid_result_is_not_transient() -> None:
    # A successful validation is never transient.
    acct = _make_account(secret_version="v7")
    svc = _service_returning(Credentials(tws_userid="mslvp", tws_password="pw"))

    result = await validate_account_credentials(acct, svc)

    assert result.valid is True
    assert result.transient is False
