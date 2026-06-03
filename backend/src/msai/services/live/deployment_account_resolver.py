"""Resolve a live-deploy request to one ACTIVE broker account + validate it.

This module sits between the ``/live/start-portfolio`` request and the
live-supervisor spawn. It does TWO things, composed entirely from existing
primitives (it does NOT reimplement credential resolution, prefix/mode
checking, or gateway routing):

1. :func:`resolve_active_broker_account` — map a deploy request to *exactly one*
   ACTIVE :class:`~msai.models.broker_account.BrokerAccount`, either by its row
   UUID or by ``ib_account_id``. The partial-unique index
   ``uq_broker_accounts_active_ib_account_id`` guarantees at most one ACTIVE row
   per ``ib_account_id``, so the ``ib_account_id`` lookup is unambiguous.

2. A TWO-STAGE validation gate. The split is deliberate and load-bearing for the
   caller's ordering (wired in Task 5):

   * :func:`validate_account_row_state` — STAGE 1. CHEAP, pure, side-effect-free
     row-state + route checks (account not archived, mode consistent with the
     account prefix, gateway route exists, account bound to its login). Reads NO
     credentials and performs NO commit, so it is safe to run EARLY — before the
     idempotency hash is computed / the reservation is taken.

   * :func:`validate_account_credentials` — STAGE 2. The credential READ. It
     calls :meth:`BrokerAccountService.resolve_for_spawn` with
     ``stamp_access=False`` (council 2026-06-01, bounded Option B): it reads the
     credential store to PROVE resolvability but performs **NO commit** on the
     request session, holds **NO DB row lock**, and does **NOT** stamp
     ``credentials_last_accessed``. It still runs LATE — after the idempotency
     reservation — so a retried/duplicate request does not repeatedly poke Key
     Vault. The mid-handler commit it used to do on the SHARED session was
     releasing any ``FOR UPDATE`` row lock held before it (a broken lock
     invariant); the deploy-gate now validates credentials with no lock held and
     no commit, and the caller (``api/live.py``) defers the
     ``credentials_last_accessed`` stamp + ``KV_SECRET_AGE`` gauge to its FINAL
     transaction, atomic with the deployment upsert. ``SPAWN_FAILED`` is still
     incremented inside ``resolve_for_spawn`` on a resolution failure.

What this gate PROVES vs does NOT prove
---------------------------------------
The gate proves the account is **active**, **mode-consistent** (its trading mode
matches its IB prefix), **route-resolvable** (a gateway route exists for its
login + the account is bound to it), and that its credentials **RESOLVE** (the
backend returns material for THE account's own ``credentials_secret_ref``, and
non-legacy rows are version-pinned). It does **NOT** verify that the resolved
TWS username matches the routing key — ``ib_login_key`` is a routing ALIAS (e.g.
``lvp`` / ``hvp``), not the TWS username, so for every migrated account the real
``tws_userid`` legitimately differs and such a check would be meaningless. It
also does **NOT** prove that the IB Gateway actually authenticated with those
credentials — that only happens when the supervisor spawns the TradingNode and
the gateway accepts the login. A green gate followed by a gateway auth failure is
expected and handled downstream, not here.

Secret hygiene: validation reasons are coarse machine codes
(``KvFailureReason`` values or ``"mode_inconsistent"`` / ``"route_not_found"`` /
``"not_router_bound"`` / ``"archived"``). The resolved ``tws_userid`` /
``tws_password`` are NEVER placed in a reason string or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from msai.models.broker_account import (
    BrokerAccount,
    BrokerAccountStatus,
    CredentialsBackend,
)
from msai.services.live.broker_account_service import AccountArchivedError
from msai.services.live.broker_credentials_store import (
    CredentialResolutionError,
    is_transient_kv_reason,
)
from msai.services.nautilus.ib_port_validator import assert_account_mode_consistent
from msai.services.observability.broker_account_metrics import DEPLOY_VALIDATION_FAILED

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.live.broker_account_service import BrokerAccountService
    from msai.services.live.gateway_router import GatewayRouter


class AccountNotResolvable(RuntimeError):  # noqa: N818 — public name fixed by Task 3 spec
    """No single ACTIVE broker account matches the deploy request.

    Raised by :func:`resolve_active_broker_account` when neither a row UUID nor
    an ``ib_account_id`` resolves to an ACTIVE row (unknown id, or only an
    ARCHIVED row exists for that ``ib_account_id``).
    """


@dataclass(frozen=True)
class DeployValidation:
    """Outcome of a validation stage.

    ``valid`` is the gate result. ``reason`` is a coarse machine code on failure
    (never a secret), ``None`` on success. ``version`` is the pinned credential
    secret version surfaced by STAGE 2 on success — ``None`` for ``legacy_env``
    rows (whose version is legitimately NULL) and always ``None`` from STAGE 1
    (which never reads credentials).

    ``transient`` is True only for a STAGE-2 credential failure whose reason is a
    RETRYABLE Key Vault infra blip (throttled / unreachable — see
    :data:`~msai.services.live.broker_credentials_store.TRANSIENT_KV_REASONS`).
    The caller maps a transient failure to HTTP 503 (retryable) and a permanent
    failure to 422. It is always False on success and for row-state rejections
    (``archived`` / ``mode_inconsistent`` / ``route_not_found`` /
    ``not_router_bound``), which are permanent.
    """

    valid: bool
    reason: str | None = None
    version: str | None = None
    transient: bool = False
    current_version: str | None = None
    """The freshly-locked ``credentials_secret_version`` surfaced by STAGE 3
    (:func:`lock_and_assert_account_active`). It is the value read UNDER the
    ``FOR UPDATE`` lock with ``populate_existing=True`` — i.e. it reflects any
    rotation/archive COMMITTED by another session, never a stale identity-map
    cache. ``None`` for ``legacy_env`` rows (whose version is legitimately NULL)
    and for every non-STAGE-3 ``DeployValidation``. The caller compares it to
    STAGE 2's :attr:`version` to detect a credential rotation that committed
    mid-deploy (a rotation race)."""


async def resolve_active_broker_account(
    db: AsyncSession,
    *,
    broker_account_id: UUID | None,
    ib_account_id: str | None,
) -> BrokerAccount:
    """Resolve a deploy request to exactly one ACTIVE :class:`BrokerAccount`.

    Exactly one of ``broker_account_id`` (the row PRIMARY KEY UUID, not the IB
    account string) or ``ib_account_id`` should be provided; ``broker_account_id``
    takes precedence when both are given.

    The ``ib_account_id`` path filters on ``status == ACTIVE`` in the query (the
    ``uq_broker_accounts_active_ib_account_id`` partial index guarantees that
    match is unique). The UUID path loads by primary key and then rejects a row
    that is not ACTIVE — so a stale/archived UUID does not slip through.

    Raises:
        AccountNotResolvable: No ACTIVE row matches, or neither key was provided.
    """
    if broker_account_id is not None:
        acct = await db.get(BrokerAccount, broker_account_id)
        if acct is None or acct.status != BrokerAccountStatus.ACTIVE:
            raise AccountNotResolvable(f"no active broker account for id {broker_account_id}")
        return acct

    if ib_account_id is not None:
        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.ib_account_id == ib_account_id,
                BrokerAccount.status == BrokerAccountStatus.ACTIVE,
            )
        )
        acct = result.scalar_one_or_none()
        if acct is None:
            raise AccountNotResolvable(
                f"no active broker account for ib_account_id {ib_account_id!r}"
            )
        return acct

    raise AccountNotResolvable("a broker_account_id or ib_account_id is required")


def validate_account_row_state(
    account: BrokerAccount,
    *,
    requested_mode: str,
    gateway_router: GatewayRouter,
) -> DeployValidation:
    """STAGE 1 — cheap, side-effect-free row-state + route validation.

    Performs NO credential read and NO DB commit, so it is safe to run EARLY in
    the deploy pipeline (before the idempotency reservation). Checks, in order:

    1. The account is not ARCHIVED.
    2. ``requested_mode`` is consistent with the account's IB prefix
       (delegated to :func:`assert_account_mode_consistent`).
    3. A gateway route exists for the account's ``ib_login_key``
       (delegated to ``gateway_router.resolve`` — raises on unknown login).
    4. If the login has a non-empty account binding, the account's
       ``ib_account_id`` is in it. An empty binding (3-tuple ``login:host:port``
       config) means "not enforced" and passes.

    Returns a ``DeployValidation`` with ``version=None`` on success (this stage
    never reads the credential version). The proof boundary is identity + route
    only — it does NOT prove credential resolvability (STAGE 2) nor gateway
    authentication (supervisor spawn).
    """
    if account.status == BrokerAccountStatus.ARCHIVED:
        return DeployValidation(valid=False, reason="archived")

    try:
        assert_account_mode_consistent(account.ib_account_id, requested_mode)
    except ValueError:
        return DeployValidation(valid=False, reason="mode_inconsistent")

    try:
        gateway_router.resolve(account.ib_login_key)
    except ValueError:
        return DeployValidation(valid=False, reason="route_not_found")

    bound_accounts = gateway_router.accounts_for(account.ib_login_key)
    if bound_accounts and account.ib_account_id not in bound_accounts:
        return DeployValidation(valid=False, reason="not_router_bound")

    return DeployValidation(valid=True)


async def validate_account_credentials(
    account: BrokerAccount,
    broker_account_service: BrokerAccountService,
) -> DeployValidation:
    """STAGE 2 — credential RESOLVABILITY validation (deploy-gate semantics).

    Backend-readability boundary (P1, real-money deploy path)
    ---------------------------------------------------------
    This gate runs in the BACKEND API process. The credential read it performs is
    only meaningful for backends the backend can actually read:

    * ``azure_kv`` — the backend's managed identity can read Key Vault → READ.
    * ``env`` (dev file-backed store) — the backend has the store file mounted →
      READ.
    * ``legacy_env`` — the migrated LVP/HVP backfill whose ``credentials_secret_ref``
      points at gateway env keys (``env:TWS_USERID|TWS_PASSWORD``). Those env vars
      live ONLY in the IB Gateway containers (docker-compose injects ``TWS_*`` into
      the ``ib-gateway`` services, NEVER into the backend / ``*worker-env``
      anchor), so the backend process CANNOT read them. Performing the read here
      would always raise credential-not-found and fail-close a deployable account
      with a spurious 422 — making LVP/HVP undeployable.

    Therefore, for ``legacy_env`` accounts this stage SKIPS ``resolve_for_spawn``
    entirely and returns ``DeployValidation(valid=True, version=None)``. The deploy
    gate validates a ``legacy_env`` account at the ROW-STATE level only (active /
    mode-consistent / route-resolvable / router-bound — already enforced in STAGE 1
    ``validate_account_row_state`` BEFORE this call, regardless of backend) and
    DEFERS credential resolvability to the IB Gateway, which is the credential
    authority for these rows: it authenticates with the ``TWS_*`` env credentials at
    spawn. A green gate followed by a gateway auth failure is expected and handled
    downstream — exactly as for the other backends, whose green gate also does not
    prove gateway authentication. ``credentials_validated_version`` stays ``None``
    for ``legacy_env`` (its version is legitimately NULL), so STAGE 3's rotation
    compare is ``None == None`` (no false trip) and the ``KV_SECRET_AGE`` gauge is
    already skipped for ``legacy_env`` (no ``credentials_updated_at``).

    For ``azure_kv`` and ``env`` it proceeds with the credential read below.

    Calls :meth:`BrokerAccountService.resolve_for_spawn` with
    ``stamp_access=False`` (council 2026-06-01, bounded Option B): it reads the
    credential store to PROVE resolvability but performs **NO commit** on the
    request session and does **NOT** stamp ``credentials_last_accessed`` — so the
    KV read happens with **no DB row lock held** and the request session's
    transaction (and any lock the caller later acquires) is never released by a
    mid-handler commit. The success-path ``credentials_last_accessed`` stamp + the
    ``KV_SECRET_AGE`` gauge are deferred to the caller's FINAL transaction, atomic
    with the deployment upsert. ``SPAWN_FAILED`` is still incremented inside
    ``resolve_for_spawn`` on a resolution failure (unchanged).

    This stage still runs LATE in the deploy pipeline — *after* the idempotency
    reservation — so a retried/duplicate deploy request does not repeatedly read
    Key Vault, even though it no longer advances the access timestamp itself.

    On success it surfaces the version that ``resolve_for_spawn`` ACTUALLY read
    against (``ResolvedSpawnCredentials.version_used``; ``None`` for
    ``legacy_env`` rows, whose version is legitimately NULL). It MUST NOT re-read
    ``account.credentials_secret_version`` here — that ORM attribute reflects
    post-commit row state and a concurrent rotation could have advanced it to a
    NEWER version than the one this request resolved, which would defeat the
    STAGE-3 ``current_version`` rotation guard (it would compare the new version
    against itself).

    The credential's provenance is already guaranteed by THE account row's own
    ``credentials_secret_ref`` (we resolve the secret that the account itself
    points at), so this stage does NOT compare the resolved ``tws_userid`` to the
    account's ``ib_login_key``. ``ib_login_key`` is a gateway-routing ALIAS (e.g.
    ``lvp`` / ``hvp``), NOT the TWS username — for every migrated account the
    resolved ``tws_userid`` (the real IB login, e.g. ``marin1016``) legitimately
    DIFFERS from the alias, so a userid↔login-key equality check would wrongly
    fail-closed valid accounts while adding no real guarantee.

    Failure reasons are coarse machine codes only — the resolved credential
    material is NEVER echoed into a reason or logged here.

    This stage proves credential RESOLVABILITY, NOT gateway authentication: that
    the backend returned material for the account's own secret ref does not prove
    IB Gateway will accept it. That is verified only at supervisor spawn time.
    """
    # P1: the backend process cannot read a legacy_env account's gateway-env
    # credentials (TWS_USERID/TWS_PASSWORD live only in the IB Gateway containers),
    # so do NOT attempt the env read here — it would always fail-close LVP/HVP with
    # a spurious 422. Row-state (active/mode/route/binding) is already validated by
    # STAGE 1; credential resolvability is deferred to the gateway at spawn (the
    # credential authority for these rows). version stays None (legacy_env version
    # is legitimately NULL → STAGE 3 None==None, no false rotation trip).
    if account.credentials_backend == CredentialsBackend.LEGACY_ENV:
        return DeployValidation(valid=True, reason=None, version=None)

    try:
        resolved = await broker_account_service.resolve_for_spawn(account.id, stamp_access=False)
    except AccountArchivedError:
        # Defensive: the account was archived between STAGE 1 and STAGE 2.
        # Not counted anywhere else (CredentialResolutionError is the only
        # resolve_for_spawn failure SPAWN_FAILED counts) — emit the alert
        # signal here so the rejection is observable.
        DEPLOY_VALIDATION_FAILED.inc(account_id=account.ib_account_id, reason="archived")
        return DeployValidation(valid=False, reason="archived")
    except CredentialResolutionError as exc:
        # ``exc.reason`` is a KvFailureReason (coarse machine code); the secret
        # material is not present on the exception and must never be echoed.
        # This failure is ALREADY counted by SPAWN_FAILED inside
        # resolve_for_spawn — do NOT also increment DEPLOY_VALIDATION_FAILED
        # (no double-count). A TRANSIENT reason (KV throttled/unreachable) is a
        # retryable infra blip — flag it so the caller returns 503, not 422.
        return DeployValidation(
            valid=False,
            reason=exc.reason.value,
            transient=is_transient_kv_reason(exc.reason),
        )

    return DeployValidation(valid=True, version=resolved.version_used)


async def lock_and_assert_account_active(
    db: AsyncSession,
    account_id: UUID,
) -> DeployValidation:
    """STAGE 3 — serialize against concurrent archive AND credential rotation.

    Takes a row-level ``SELECT ... FOR UPDATE`` on the ``broker_accounts`` row and
    re-reads it UNDER that lock with ``populate_existing=True`` so BOTH ``status``
    and ``credentials_secret_version`` are the FRESH, freshly-locked DB values —
    NOT attributes stale-cached in this session's identity map (a prior STAGE-2
    read / reused ORM instance). Returns ``valid=False, reason="archived"`` if the
    row is gone or no longer ACTIVE; otherwise ``valid=True`` with
    ``current_version`` = the locked row's current ``credentials_secret_version``
    (``None`` for ``legacy_env`` rows). The caller compares ``current_version`` to
    the version STAGE 2 validated to detect a rotation that committed mid-deploy.

    This closes TWO TOCTOU races. STAGE 2's credential resolve commits (releasing
    any prior locks), so an operator can archive the account OR rotate its
    credentials *after* validation but *before* the deployment row is inserted.
    Holding this ``FOR UPDATE`` lock from here through the deployment-upsert commit
    serializes the two operations on the same row:

    * If START commits first, it holds the lock; :meth:`BrokerAccountService.archive`
      blocks on the lock, and once START commits there is an active deployment so
      archive's own ``no active deployment`` guard fires (409).
    * If ARCHIVE commits first, START blocks here, then re-reads the now-ARCHIVED
      status under the lock and fails closed BEFORE inserting the deployment row
      or publishing START.
    * If a ROTATION commits first, START blocks here, re-reads the FRESH
      ``credentials_secret_version`` under the lock, and the caller sees it differ
      from the validated version → fail closed retryable (re-validate against the
      new version), BEFORE inserting the row or publishing START.

    The lock MUST be acquired inside the SAME unit of work that performs the
    deployment upsert (after STAGE 2's commit) and held until that upsert commits.
    On rejection the caller increments ``DEPLOY_VALIDATION_FAILED`` (this helper
    does not, so the caller controls the metric label) and fails closed.
    """
    # ``populate_existing=True`` forces the locked row's columns to OVERWRITE any
    # identity-map-cached attributes — so ``status`` AND ``credentials_secret_version``
    # are the freshly-locked DB values, reflecting a rotation/archive committed by
    # another session in the STAGE-2→STAGE-3 window (not a stale ORM cache).
    locked = (
        await db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None or locked.status != BrokerAccountStatus.ACTIVE:
        return DeployValidation(valid=False, reason="archived")
    return DeployValidation(valid=True, current_version=locked.credentials_secret_version)
