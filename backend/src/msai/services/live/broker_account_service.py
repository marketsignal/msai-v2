"""BrokerAccountService — create/list/get/update/rotate/archive of broker accounts.

The service owns the *control-plane* lifecycle of a :class:`BrokerAccount`
row plus the credential secret material it references. The secret itself
NEVER lives in the row — it is written to a :class:`BrokerCredentialsStore`
(Azure Key Vault in prod, a file-backed env store in dev) and the row keeps
only the ``credentials_secret_ref`` + ``credentials_secret_version`` +
audit columns.

Key invariants (see ``docs/plans/2026-06-01-broker-account-entity.md`` Task 9):

* ``create`` writes the secret to the store FIRST, then INSERTs the row
  inside a *bounded* slot-allocation retry loop. The two partial-unique
  indexes (``uq_broker_accounts_active_ib_account_id`` /
  ``uq_broker_accounts_active_gateway_slot``) arbitrate concurrent inserts;
  a slot conflict triggers a re-read + retry, an ``ib_account_id`` conflict
  is terminal (:class:`DuplicateAccountError`). Any terminal allocation
  failure deletes the just-written secret so no orphan is left behind.
* ``rotate`` writes the new secret version to the store FIRST; only on
  success does it stamp the new version + audit columns onto the row
  (half-rotation safety — if the store raises, the row is untouched).
* ``update`` mutates ``label`` / ``trading_mode`` only. ``ib_account_id``
  is IMMUTABLE — attempting to change it raises :class:`ImmutableFieldError`.
* ``archive`` first blocks if ANY deployment bound to this account is in an
  active lifecycle state (:data:`ACTIVE_DEPLOYMENT_STATUSES`) — raises
  :class:`AccountInUseError` (→ 409) and does NOT touch the secret. Otherwise
  it flips ``status`` to archived (freeing the slot via the partial index),
  then deletes the secret (skipped for ``legacy_env`` rows whose material is
  compose env we do not own).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from msai.core.logging import get_logger
from msai.core.secrets import EnvSecretsProvider
from msai.models.broker_account import (
    BrokerAccount,
    BrokerAccountStatus,
    CredentialsBackend,
)
from msai.models.live_deployment import LiveDeployment
from msai.services.live.broker_credentials_store import (
    CredentialResolutionError,
    Credentials,
    KvFailureReason,
    ResolvedSpawnCredentials,
)
from msai.services.nautilus.ib_port_validator import assert_account_mode_consistent
from msai.services.observability.broker_account_metrics import KV_SECRET_AGE, SPAWN_FAILED

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.live.broker_credentials_store import (
        BrokerCredentialsStore,
        CredentialWriteResult,
    )

# Mirrors the fleet_router authoritative active set and the archive-guard
# invariant (Codex iter-1 P1#5 — NOT just "running"). Includes ``stopping``:
# a node mid-teardown still holds IB positions (nautilus gotcha #13), so it
# counts as active for both the archive guard and the boot KV-reachability
# probe in ``main.py``.
ACTIVE_DEPLOYMENT_STATUSES: tuple[str, ...] = (
    "running",
    "ready",
    "starting",
    "building",
    "stopping",
)

log = get_logger(__name__)

# Partial-unique index names from migration ``d87c2aa5f751``. Used to tell an
# ``ib_account_id`` conflict apart from a ``gateway_slot`` conflict — and BOTH
# apart from any other constraint violation (e.g. the ``created_by`` FK), which
# must NOT be mislabeled as a slot race.
_IB_ACCOUNT_ID_CONSTRAINT = "uq_broker_accounts_active_ib_account_id"
_GATEWAY_SLOT_CONSTRAINT = "uq_broker_accounts_active_gateway_slot"


class _Unset(Enum):
    """Sentinel distinguishing 'field omitted' from an explicit ``None``.

    ``update(label=...)`` must apply ``label=None`` ONLY when the caller actually
    sent the key (so a PATCH ``{"label": null}`` can CLEAR a label), while leaving
    the stored label untouched when the field is omitted. A plain ``None`` default
    cannot express both, so the router passes ``UNSET`` for an omitted field
    (Pydantic ``model_fields_set`` drives this) and the explicit value otherwise.
    """

    UNSET = "unset"


UNSET = _Unset.UNSET


class BrokerAccountError(RuntimeError):
    """Base class for all broker-account service errors."""


class NoFreeSlotError(BrokerAccountError):
    """No free gateway slot remains in the configured pool."""


class DuplicateAccountError(BrokerAccountError):
    """An ACTIVE broker account already exists for this ``ib_account_id``."""


class AccountInUseError(BrokerAccountError):
    """The account has a deployment in an active lifecycle state."""


class ImmutableFieldError(BrokerAccountError):
    """An attempt was made to mutate an immutable field (e.g. ``ib_account_id``)."""


class AccountNotFoundError(BrokerAccountError):
    """No broker account exists for the given id."""


class AccountArchivedError(BrokerAccountError):
    """A mutation (update/rotate/resolve) was attempted on an ARCHIVED account.

    Archived is terminal: the gateway slot is freed and the managed secret is
    deleted, so editing, rotating, or spawning from an archived row is rejected
    (plan US-003 edge case). Maps to 409 at the API boundary.
    """


def _matches_constraint(exc: IntegrityError, constraint: str) -> bool:
    """Return True iff ``exc`` is a violation of the named ``constraint``.

    Inspects the asyncpg ``constraint_name`` attribute first (the precise signal —
    mirrors the constraint-name inspection used in the live_supervisor
    fleet_router IntegrityError handling), then falls back to a substring match on
    the rendered exception so the check is robust across drivers.

    The ``constraint_name`` path is authoritative: when the driver reports a
    constraint name, a NON-match returns ``False`` even if the rendered string
    happens to mention ``constraint`` — so a violation of a DIFFERENT constraint
    (e.g. the ``created_by`` FK) is never mislabeled as one of the broker-account
    partial-unique indexes.
    """
    # SQLAlchemy wraps the DBAPI error in ``.orig``; asyncpg's UniqueViolationError
    # is reachable via that object (directly or through ``__cause__``) and carries
    # a ``constraint_name`` attribute.
    candidates = [exc.orig, getattr(exc.orig, "__cause__", None)]
    for candidate in candidates:
        name = getattr(candidate, "constraint_name", None)
        if name:
            return bool(name == constraint)
    return constraint in str(exc.orig)


def _is_account_id_conflict(exc: IntegrityError) -> bool:
    """Return True iff ``exc`` is the ``ib_account_id`` partial-unique violation.

    The ``uq_broker_accounts_active_ib_account_id`` constraint maps to
    :class:`DuplicateAccountError` (terminal).
    """
    return _matches_constraint(exc, _IB_ACCOUNT_ID_CONSTRAINT)


def _is_slot_conflict(exc: IntegrityError) -> bool:
    """Return True iff ``exc`` is the ``gateway_slot`` partial-unique violation.

    The ``uq_broker_accounts_active_gateway_slot`` constraint is the slot race
    (retryable for an unpinned create, terminal "slot in use" for a pinned one).
    Symmetric with :func:`_is_account_id_conflict` so the create() handler can
    discriminate the slot race from BOTH the duplicate-account case AND any other
    constraint violation — the latter must re-raise unchanged, never loop into a
    misleading :class:`NoFreeSlotError`.
    """
    return _matches_constraint(exc, _GATEWAY_SLOT_CONSTRAINT)


class BrokerAccountService:
    """Control-plane lifecycle for :class:`BrokerAccount` rows + their secrets."""

    def __init__(
        self,
        db: AsyncSession,
        store: BrokerCredentialsStore,
        slots: list[str],
        backend: CredentialsBackend = CredentialsBackend.ENV,
    ) -> None:
        self._db = db
        self._store = store
        self._slots = slots
        self._backend = backend

    async def _free_slot(self) -> str:
        """Return the first slot in the pool not held by an ACTIVE row."""
        rows = await self._db.execute(
            select(BrokerAccount.gateway_slot).where(
                BrokerAccount.status != BrokerAccountStatus.ARCHIVED
            )
        )
        taken = {r[0] for r in rows}
        for slot in self._slots:
            if slot not in taken:
                return slot
        raise NoFreeSlotError(f"no free gateway slot in pool {self._slots}")

    def _best_effort_delete_secret(self, secret_ref: str, *, reason: str) -> None:
        """Delete a secret, swallowing :class:`CredentialResolutionError`.

        Used in cleanup/teardown paths where the secret delete is best-effort:
        a store failure here must NOT mask the primary exception (create
        cleanup) nor abort an already-committed state transition (archive).
        The failure is logged loudly so an orphaned secret can be GC'd by hand.
        """
        try:
            self._store.delete(secret_ref)
        except CredentialResolutionError as exc:
            log.error(
                "broker_account_secret_delete_failed",
                secret_ref=secret_ref,
                reason=str(exc.reason),
                context=reason,
            )

    async def create(
        self,
        *,
        ib_account_id: str,
        ib_login_key: str,
        trading_mode: str,
        gateway_slot: str | None,
        creds: Credentials,
        actor: str,
        label: str | None = None,
        created_by: UUID | None = None,
    ) -> BrokerAccount:
        """Create an ACTIVE broker account: write secret first, then allocate a slot.

        ``created_by`` is the resolved ``users.id`` of the operator who issued the
        create (the router resolves the JWT subject to a user row and passes it
        through). It is persisted to the audit column; ``None`` leaves it NULL.
        """
        # Pre-check for a duplicate ACTIVE account (the partial-unique index is the
        # authoritative arbiter against races; this is a fast, friendly pre-check).
        existing = await self._db.execute(
            select(BrokerAccount).where(
                BrokerAccount.ib_account_id == ib_account_id,
                BrokerAccount.status != BrokerAccountStatus.ARCHIVED,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateAccountError(f"active account {ib_account_id} already exists")
        if gateway_slot and gateway_slot not in self._slots:
            raise BrokerAccountError(f"unknown gateway slot {gateway_slot}")

        new_id = uuid4()
        secret_ref = f"broker-cred-{new_id}"
        write = self._store.put(secret_ref, creds, actor=actor)  # STORE FIRST

        # --- Pre-commit region (cleanup-eligible) --------------------------------
        # Everything up to and INCLUDING a SUCCESSFUL commit is the orphan-secret
        # cleanup boundary: if allocation or the row INSERT fails before the row is
        # durable, the just-written secret has no owning row and MUST be deleted.
        # Once commit() returns, the row is durable and we cross the durable
        # boundary — NO path below deletes the secret (a post-commit refresh blip
        # must never orphan a live account's credentials).
        #
        # ``_allocate_and_insert`` isolates each slot attempt in a SAVEPOINT
        # (``begin_nested`` + ``flush``) so a slot-race rollback discards ONLY the
        # broker_account attempt — it does NOT touch the outer transaction nor any
        # pending sibling row (e.g. a first-time-user ``users`` row the router
        # flushed before calling create() and whose id is our ``created_by`` FK).
        # The single durable commit is issued HERE, after a slot is won, so it
        # persists BOTH that pending user and the broker_account atomically.
        try:
            acct = await self._allocate_and_insert(
                new_id=new_id,
                ib_account_id=ib_account_id,
                ib_login_key=ib_login_key,
                trading_mode=trading_mode,
                gateway_slot=gateway_slot,
                write=write,
                actor=actor,
                label=label,
                created_by=created_by,
            )
            await self._db.commit()
        except BrokerAccountError:
            # Terminal allocation failure (DuplicateAccountError / NoFreeSlotError /
            # pinned "slot in use") — delete the orphaned secret. Best-effort: a
            # store-delete failure must NOT mask the primary error (which maps to
            # the correct 409/422, not a misleading 502).
            self._best_effort_delete_secret(secret_ref, reason="create_terminal_cleanup")
            raise
        except Exception:
            # Transient row-INSERT/commit failure OR an unrelated IntegrityError
            # re-raised unchanged (e.g. a created_by FK violation) — best-effort:
            # no orphan secret. Roll back so any partially-staged state is
            # discarded; a redundant rollback is harmless and also covers any
            # other pre-commit exception.
            await self._db.rollback()
            self._best_effort_delete_secret(secret_ref, reason="create_transient_cleanup")
            raise

        # --- Durable boundary crossed: row committed -----------------------------
        # A refresh failure here is a transient post-commit blip; the row is
        # already durable and its secret MUST survive. Re-load best-effort; if even
        # the re-load raises, propagate it — but NEVER delete the secret.
        await self._db.refresh(acct)
        return acct

    async def _allocate_and_insert(
        self,
        *,
        new_id: UUID,
        ib_account_id: str,
        ib_login_key: str,
        trading_mode: str,
        gateway_slot: str | None,
        write: CredentialWriteResult,
        actor: str,
        label: str | None,
        created_by: UUID | None,
    ) -> BrokerAccount:
        """Allocate a slot and INSERT the row inside the bounded retry loop.

        Returns the staged (flushed but NOT-yet-committed, not-yet-refreshed) row;
        the caller (:meth:`create`) issues the single durable commit. Raises a
        :class:`BrokerAccountError` subclass for a terminal allocation failure, or
        re-raises an unrelated :class:`IntegrityError` UNCHANGED. The caller owns
        the orphan-secret cleanup for any exception raised here (all pre-commit).

        Each attempt is isolated in a SAVEPOINT (``begin_nested`` + ``flush``):
        the flush triggers the partial-unique indexes WITHOUT committing the outer
        transaction, and on conflict the ``begin_nested`` block rolls back ONLY to
        the savepoint — so a slot race discards just this broker_account attempt
        and leaves the outer transaction (plus any pending sibling row such as a
        freshly-flushed first-time-user ``users`` row referenced by ``created_by``)
        fully intact. This is the transaction-isolation fix: a session-wide
        ``rollback()`` here would have dropped that pending user and turned the
        retry's INSERT into an FK violation.
        """
        # Bounded slot-allocation retry (Codex iter-1 P1#6): the read-then-insert
        # is racy, so let the partial-unique index arbitrate and retry on conflict.
        for _attempt in range(len(self._slots) + 1):
            slot = gateway_slot or await self._free_slot()  # raises NoFreeSlotError
            acct = BrokerAccount(
                id=new_id,
                ib_account_id=ib_account_id,
                ib_login_key=ib_login_key,
                label=label,
                status=BrokerAccountStatus.ACTIVE,
                gateway_slot=slot,
                trading_mode=trading_mode,
                credentials_backend=self._backend,
                credentials_secret_ref=write.secret_ref,
                credentials_secret_version=write.version,
                credentials_updated_at=datetime.now(UTC),
                credentials_updated_by=actor,
                created_by=created_by,
            )
            try:
                # SAVEPOINT: the flush triggers the partial-unique constraints;
                # on conflict the block rolls back to the savepoint (discarding
                # only this acct), leaving the outer transaction untouched.
                async with self._db.begin_nested():
                    self._db.add(acct)
                    await self._db.flush()
            except IntegrityError as exc:
                if _is_account_id_conflict(exc):
                    raise DuplicateAccountError(
                        f"active account {ib_account_id} already exists"
                    ) from exc
                if _is_slot_conflict(exc):
                    if gateway_slot is not None:  # caller pinned a slot that's now taken
                        raise BrokerAccountError(f"gateway slot {gateway_slot} is in use") from exc
                    continue  # slot race — re-read a free slot and retry
                # Any OTHER constraint violation (e.g. the created_by FK) is NOT a
                # slot race: re-raise the ORIGINAL IntegrityError unchanged — never
                # loop into a misleading NoFreeSlotError nor mislabel it "slot in use".
                raise
            return acct
        raise NoFreeSlotError(f"no free gateway slot after retries in pool {self._slots}")

    async def list(self, *, include_archived: bool = False) -> list[BrokerAccount]:
        """Return broker accounts, newest first. Excludes archived by default."""
        stmt = select(BrokerAccount).order_by(BrokerAccount.created_at.desc())
        if not include_archived:
            stmt = stmt.where(BrokerAccount.status != BrokerAccountStatus.ARCHIVED)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get(self, account_id: UUID) -> BrokerAccount:
        """Return the account by id, or raise :class:`AccountNotFoundError`."""
        acct = await self._db.get(BrokerAccount, account_id)
        if acct is None:
            raise AccountNotFoundError(f"no broker account {account_id}")
        return acct

    async def update(
        self,
        account_id: UUID,
        *,
        label: str | None | Literal[_Unset.UNSET] = UNSET,
        trading_mode: str | None | Literal[_Unset.UNSET] = UNSET,
        ib_account_id: str | None = None,
    ) -> BrokerAccount:
        """Update mutable fields (``label`` / ``trading_mode``) only.

        ``label`` and ``trading_mode`` default to the :data:`UNSET` sentinel,
        which means 'field omitted — leave it unchanged'. An explicit ``None``
        for ``label`` CLEARS the stored label (PATCH ``{"label": null}``); the
        router derives 'omitted vs explicit-null' from the request's
        ``model_fields_set`` and only forwards a key the caller actually sent.

        ``ib_account_id`` is IMMUTABLE — passing a value (even the current one)
        raises :class:`ImmutableFieldError`.
        """
        if ib_account_id is not None:
            raise ImmutableFieldError("ib_account_id is immutable and cannot be changed")
        acct = await self.get(account_id)
        if acct.status == BrokerAccountStatus.ARCHIVED:
            raise AccountArchivedError(
                f"account {acct.ib_account_id} is archived and cannot be edited"
            )
        if label is not UNSET:
            acct.label = label
        if trading_mode is not UNSET:
            # trading_mode is non-nullable; the router never forwards an explicit
            # null (the schema types it str | None but a null is meaningless and
            # would violate the column). Guard defensively.
            if trading_mode is None:
                raise BrokerAccountError("trading_mode cannot be null")
            # Enforce the SAME prefix-vs-mode guard as create (iter-4 P2): a
            # DU/DF paper account must never be PATCHed to live, nor a U... live
            # account to paper — that would silently misroute orders at the
            # gateway (nautilus gotcha #6). Checked against the row's IMMUTABLE
            # ib_account_id via the single shared helper so create + update can't
            # drift. A mismatch maps to 422 via the router catch-all.
            try:
                assert_account_mode_consistent(acct.ib_account_id, trading_mode)
            except ValueError as exc:
                raise BrokerAccountError(str(exc)) from exc
            acct.trading_mode = trading_mode
        await self._db.commit()
        await self._db.refresh(acct)
        return acct

    async def rotate(
        self,
        account_id: UUID,
        *,
        creds: Credentials,
        actor: str,
    ) -> BrokerAccount:
        """Rotate the account's credentials: store first, then update the row.

        If the store raises, the row is left untouched (half-rotation safety).

        A ``legacy_env`` row is PROMOTED to the managed backend on rotation:
        the old ``env:<USERID_KEY>|<PASSWORD_KEY>`` pointer cannot be rotated
        (we don't own the compose env), so we write the fresh credentials to a
        managed ``broker-cred-<id>`` ref in the store and stamp the row's
        backend + ref + version. After this, ``resolve_for_spawn`` reads via
        the store, no longer the env path.
        """
        acct = await self.get(account_id)
        # Lock the row + refresh UNDER the lock so the archived-check AND the
        # ``credentials_backend`` promotion branch below read state reflecting any
        # concurrent archive()/rotate() commit — not a stale identity-map cache.
        # Without the lock, a rotate could promote (write a managed secret to) an
        # account another session just archived, orphaning the secret and
        # violating the archived invariant. Same FOR UPDATE + populate_existing
        # discipline as archive() and the deploy gate's lock_and_assert_account_active.
        acct = (
            await self._db.execute(
                select(BrokerAccount)
                .where(BrokerAccount.id == acct.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if acct.status == BrokerAccountStatus.ARCHIVED:
            # Capture before rollback (rollback expires ORM attributes), then
            # release the row lock so a caller catching AccountArchivedError with
            # an open session does not block subsequent start/archive/rotate.
            ib_account_id = acct.ib_account_id
            await self._db.rollback()
            raise AccountArchivedError(f"account {ib_account_id} is archived and cannot be rotated")
        # Track a freshly-PROMOTED secret ref so a commit failure on the
        # legacy→managed branch does not leave an orphan secret with no row
        # pointing at it (the row stays legacy_env on rollback). The
        # store.rotate branch is exempt: a rollback there leaves only a bounded
        # extra secret version under the SAME ref the row still references —
        # acceptable, and not safely deletable without dropping the live version.
        promoted_ref: str | None = None
        # The store write happens AFTER the FOR UPDATE lock above, so a store
        # failure must release that lock — otherwise a caller that catches the
        # error and keeps the session open blocks every subsequent
        # start/archive/rotate for this account. (Before rotate() took the lock
        # this path held none, so a store failure was harmless; the lock made the
        # rollback mandatory.) A store.put() that RAISED returns no ref we own and
        # the in-memory backend/ref mutation is discarded by the rollback, so no
        # secret cleanup is needed here — promoted-secret cleanup belongs only to
        # the post-write COMMIT-failure path below (where put() succeeded).
        try:
            if acct.credentials_backend == CredentialsBackend.LEGACY_ENV:
                # UNIQUE-per-attempt secret name (Codex final-review P2): a deterministic
                # `broker-cred-{acct.id}` would, on a commit-failure rollback, be soft-deleted
                # in Azure KV — and KV RESERVES soft-deleted names, so the operator's retry
                # (reusing the same name) would fail with ResourceExistsError until purge/recover.
                # A fresh uuid suffix per attempt is never reused, mirroring create()'s
                # unique-name invariant (research finding #1).
                new_ref = f"broker-cred-{acct.id}-{uuid4().hex}"
                write = self._store.put(new_ref, creds, actor=actor)  # STORE FIRST
                promoted_ref = write.secret_ref
                acct.credentials_backend = self._backend
                acct.credentials_secret_ref = write.secret_ref
            else:
                write = self._store.rotate(acct.credentials_secret_ref, creds, actor=actor)
            acct.credentials_secret_version = write.version
            acct.credentials_updated_at = datetime.now(UTC)
            acct.credentials_updated_by = actor
        except Exception:
            await self._db.rollback()
            raise
        try:
            await self._db.commit()
        except Exception:
            # The row INSERT/UPDATE failed to persist. Roll back so the in-memory
            # backend/ref/version mutations are discarded (row stays legacy_env),
            # and best-effort delete the just-promoted secret so it is not orphaned.
            await self._db.rollback()
            if promoted_ref is not None:
                self._best_effort_delete_secret(promoted_ref, reason="rotate_promotion_cleanup")
            raise
        await self._db.refresh(acct)
        return acct

    async def archive(self, account_id: UUID, *, actor: str) -> BrokerAccount:
        """Archive the account, freeing its gateway slot and deleting its secret.

        Blocks FIRST (before any mutation) if a deployment bound to this account
        is in an active lifecycle state — raises :class:`AccountInUseError`. The
        secret is deleted EXCEPT for ``legacy_env`` rows (compose env material we
        do not own).
        """
        acct = await self.get(account_id)

        # Serialize against a concurrent ``/live/start-portfolio`` on the SAME
        # broker_accounts row (deploy/archive TOCTOU). The start handler takes a
        # ``SELECT ... FOR UPDATE`` on this row immediately before its deployment
        # upsert and holds it until that upsert commits; we take the matching lock
        # here BEFORE the active-deployment check below, so whoever commits first
        # wins. If start commits first, the row lock blocks us until its active
        # deployment exists → the guard below fires (AccountInUseError). If we
        # commit the ARCHIVED status first, start blocks on this lock, then
        # re-reads ARCHIVED and fails closed before inserting/publishing. The lock
        # is released by this method's ``commit()`` (or rollback on error).
        #
        # Re-read the row UNDER the lock with ``populate_existing=True`` and use
        # THAT instance: the ``credentials_backend`` / ``credentials_secret_ref``
        # reads below must reflect any rotation/promotion another session committed
        # while we waited on the lock. A stale identity-map cache (from ``get()``
        # above, taken before the lock) could still read ``LEGACY_ENV`` after a
        # concurrent legacy→managed promotion, skip deleting the just-promoted
        # managed secret, and orphan it. Same discipline as the deploy gate's
        # ``lock_and_assert_account_active``.
        acct = (
            await self._db.execute(
                select(BrokerAccount)
                .where(BrokerAccount.id == acct.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        # Block on ANY active deployment for this IB account, matched by
        # ``account_id`` ALONE (Codex iter-4 P2 / archive-too-narrow): a
        # deployment under a DIFFERENT ib_login_key (e.g. legacy "default" vs
        # new "lvp") for the same IB account still holds positions and must
        # block archive. Matching on the login key too would let such a
        # deployment slip through the guard.
        active = await self._db.execute(
            select(LiveDeployment.id).where(
                LiveDeployment.account_id == acct.ib_account_id,
                LiveDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
            )
        )
        if active.first() is not None:
            # Capture the id BEFORE the rollback below — rollback expires ORM
            # attributes, so reading ``acct.ib_account_id`` afterward would
            # trigger a lazy reload on the just-rolled-back session.
            ib_account_id = acct.ib_account_id
            # Release the FOR UPDATE row lock acquired above before raising. The
            # success path commits (which releases it); this post-lock domain
            # error must roll back so a caller that catches AccountInUseError and
            # keeps the session open does not hold the broker_accounts row lock,
            # blocking every subsequent start/archive for this account (Codex P3).
            await self._db.rollback()
            raise AccountInUseError(
                f"account {ib_account_id} has an active deployment; stop it before archiving"
            )

        secret_ref = acct.credentials_secret_ref
        skip_store_delete = acct.credentials_backend == CredentialsBackend.LEGACY_ENV
        acct.status = BrokerAccountStatus.ARCHIVED
        acct.credentials_updated_at = datetime.now(UTC)
        acct.credentials_updated_by = actor
        await self._db.commit()
        # Secret delete is best-effort AFTER the ARCHIVED commit: a store
        # failure must NOT 502 nor leave the operation half-done (the row is
        # already archived). A failure is logged loudly for manual GC.
        if not skip_store_delete:
            self._best_effort_delete_secret(secret_ref, reason="archive")
        await self._db.refresh(acct)
        return acct

    async def resolve_for_spawn(
        self, account_id: UUID, *, stamp_access: bool = True
    ) -> ResolvedSpawnCredentials:
        """Resolve live credentials for the supervisor to spawn a TradingNode.

        Branches on ``credentials_backend``:

        * ``LEGACY_ENV``: the ref is a paired ``env:<USERID_KEY>|<PASSWORD_KEY>``
          pointer; BOTH keys are read from the process environment via
          :class:`EnvSecretsProvider`. A missing key fails loud
          (:data:`KvFailureReason.NOT_FOUND`). ``credentials_secret_version`` is
          NULL here and that is expected.
        * ``AZURE_KV`` / ``ENV``: a NULL ``credentials_secret_version`` fails
          closed (:data:`KvFailureReason.DECRYPT_FAILED`) — a non-legacy row MUST
          have a pinned version. Otherwise the store reads the pinned version.

        On any :class:`CredentialResolutionError` the spawn-failure counter is
        incremented (labeled by ``account_id`` + ``reason``) before re-raising.

        ``stamp_access`` controls the success-path side effects (council
        2026-06-01, bounded Option B):

        * ``True`` (default — preserves behavior for the data-plane / existing /
          test callers): stamp ``credentials_last_accessed`` (tz-aware UTC),
          ``commit()`` the request session, and set the secret-age gauge from
          ``now - credentials_updated_at`` (skipped for ``legacy_env`` rows, which
          have no ``credentials_updated_at``).
        * ``False`` (the **deploy-gate** path in ``api/live.py``): do the
          non-locking ``refresh`` + archived check + ``store.get`` and RETURN —
          with **NO ``commit()``** and **NO** write to ``credentials_last_accessed``.
          The mid-handler commit on the request's SHARED session was releasing any
          ``FOR UPDATE`` row lock held before it (the lock-invariant defect this
          fork fixes), so the deploy gate must validate credentials with no lock
          held and no commit. The CALLER then persists
          ``credentials_last_accessed`` (and sets the secret-age gauge) in its
          FINAL successful transaction, atomic with the deployment upsert.
          ``credentials_last_accessed`` is pure observability metadata — nothing
          branches on it, and a fail-closed deploy need not stamp it.

        The early ``db.refresh(acct)`` (a NON-locking ``SELECT``) is kept in both
        modes: it is the archived-race guard and runs BEFORE any lock in the
        deploy-gate's new lock ordering.

        Returns a :class:`ResolvedSpawnCredentials` carrying the resolved
        ``creds`` AND the EXACT ``version_used`` — the version passed to
        ``store.get(ref, version)`` for the read that produced ``creds`` (``None``
        for ``legacy_env``). ``version_used`` is captured AT the store-read call
        site, BEFORE the post-commit ``refresh``, so a concurrent credential
        rotation committing a NEWER version onto the row mid-resolve cannot make
        the caller believe it validated the new version. Callers MUST use
        ``version_used`` (never re-read ``credentials_secret_version`` afterwards)
        as the validated version — that is what the STAGE-3 rotation guard in
        ``api/live.py`` compares against the locked row's current version.
        """
        acct = await self.get(account_id)
        # The instance may be a CACHED ACTIVE copy from this session's identity
        # map (STAGE-1 _resolve_effective_account loaded it; a concurrent request
        # may have archived + committed it since). Re-SELECT committed state so the
        # ARCHIVED check below — and the subsequent credentials_secret_version /
        # backend reads — never trust stale ACTIVE data. This is a NON-locking
        # SELECT; in the deploy-gate (stamp_access=False) path it runs BEFORE any
        # FOR UPDATE lock is taken. broker accounts are soft-archived (never
        # hard-deleted), so the row always exists; if it were somehow gone,
        # refresh() raises and we fail closed.
        await self._db.refresh(acct)
        # An archived account must never yield usable credentials — even a
        # legacy_env row whose env secret still exists. Fail BEFORE resolving any
        # material and BEFORE stamping last_accessed.
        if acct.status == BrokerAccountStatus.ARCHIVED:
            raise AccountArchivedError(
                f"account {acct.ib_account_id} is archived and cannot be spawned"
            )
        ref = acct.credentials_secret_ref
        is_legacy = acct.credentials_backend == CredentialsBackend.LEGACY_ENV
        # The version we ACTUALLY read against. Captured here (BEFORE the store
        # read for non-legacy, and BEFORE the post-commit refresh) so it reflects
        # the version this request resolved — never a newer rotated version.
        version_used: str | None = None
        try:
            if is_legacy:
                creds = self._resolve_legacy_env(ref)
            else:
                if acct.credentials_secret_version is None:
                    raise CredentialResolutionError(
                        KvFailureReason.DECRYPT_FAILED,
                        ref,
                        "non-legacy account is missing a pinned secret version",
                    )
                version_used = acct.credentials_secret_version
                creds = self._store.get(ref, version_used)
        except CredentialResolutionError as exc:
            SPAWN_FAILED.inc(account_id=acct.ib_account_id, reason=exc.reason)
            raise

        if not stamp_access:
            # Deploy-gate path: NO commit on the shared request session and NO
            # last-accessed stamp. The caller persists credentials_last_accessed
            # + the secret-age gauge in its final transaction (atomic with the
            # deployment upsert). Returning here leaves no row lock held and no
            # pending write that would autoflush a lock between here and the
            # caller's critical section.
            return ResolvedSpawnCredentials(creds=creds, version_used=version_used)

        now = datetime.now(UTC)
        acct.credentials_last_accessed = now
        await self._db.commit()
        await self._db.refresh(acct)
        if not is_legacy and acct.credentials_updated_at is not None:
            age_seconds = (now - acct.credentials_updated_at).total_seconds()
            KV_SECRET_AGE.set(age_seconds, account_id=acct.ib_account_id)
        return ResolvedSpawnCredentials(creds=creds, version_used=version_used)

    @staticmethod
    def _resolve_legacy_env(ref: str) -> Credentials:
        """Read a paired ``env:<USERID_KEY>|<PASSWORD_KEY>`` legacy reference.

        Both env keys must be present; a missing key raises a
        :class:`CredentialResolutionError` (NOT_FOUND) naming the missing key.
        """
        spec = ref.removeprefix("env:")
        userid_key, _, password_key = spec.partition("|")
        provider = EnvSecretsProvider()
        try:
            userid = provider.get(userid_key)
        except KeyError as exc:
            raise CredentialResolutionError(
                KvFailureReason.NOT_FOUND, ref, f"missing env key {userid_key}"
            ) from exc
        try:
            password = provider.get(password_key)
        except KeyError as exc:
            raise CredentialResolutionError(
                KvFailureReason.NOT_FOUND, ref, f"missing env key {password_key}"
            ) from exc
        return Credentials(userid, password)
