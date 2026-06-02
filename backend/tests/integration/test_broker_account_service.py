"""Service-level integration tests for :class:`BrokerAccountService` (PR 3 Task 9).

Exercises the write-store-first ordering, bounded slot allocation, pool
exhaustion, archive (frees slot + deletes secret), and the
archive-blocked-while-deployment-active guard.

Runs against a dedicated Postgres testcontainer with ``alembic upgrade
head`` applied so the partial-unique indexes
(``uq_broker_accounts_active_ib_account_id`` /
``uq_broker_accounts_active_gateway_slot``) — which are defined ONLY in
the migration, not on the ORM model — actually exist. The slot-allocation
retry loop and duplicate detection depend on those partial indexes.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from msai.services.live.broker_account_service import (
    _GATEWAY_SLOT_CONSTRAINT,
    AccountArchivedError,
    AccountInUseError,
    BrokerAccountError,
    BrokerAccountService,
    DuplicateAccountError,
    NoFreeSlotError,
)
from msai.services.live.broker_credentials_store import (
    CredentialResolutionError,
    Credentials,
    EnvFileBrokerCredentialsStore,
    KvFailureReason,
)


@pytest.mark.asyncio
async def test_create_allocates_slot_writes_store_then_row(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a", "slot-b"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    assert acct.gateway_slot in ("slot-a", "slot-b")
    assert acct.credentials_secret_ref == f"broker-cred-{acct.id}"
    assert acct.credentials_secret_version is not None
    assert store.get(acct.credentials_secret_ref, acct.credentials_secret_version) == Credentials(
        "u", "p"
    )


@pytest.mark.asyncio
async def test_create_raises_when_pool_exhausted(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["only-one"])
    await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    with pytest.raises(NoFreeSlotError):
        await svc.create(
            ib_account_id="DU2",
            ib_login_key="L2",
            trading_mode="paper",
            gateway_slot=None,
            creds=Credentials("u", "p"),
            actor="op@x",
        )


class _DeleteFailsStore(EnvFileBrokerCredentialsStore):
    """Dev store whose ``delete`` always raises — exercises best-effort cleanup."""

    def delete(self, secret_ref: str) -> None:  # type: ignore[override]
        raise CredentialResolutionError(KvFailureReason.UNREACHABLE, secret_ref, "delete boom")


@pytest.mark.asyncio
async def test_create_cleanup_delete_failure_does_not_mask_primary_error(
    broker_db_session, tmp_path
):
    # finding 2: an orphan-secret cleanup store.delete that itself raises must
    # NOT mask the primary DuplicateAccountError (which maps to 409, not 502).
    store = _DeleteFailsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a", "slot-b"])
    await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    # second create for the same ib_account_id → DuplicateAccountError; the
    # cleanup delete raises CredentialResolutionError internally but is swallowed.
    with pytest.raises(DuplicateAccountError):
        await svc.create(
            ib_account_id="DU1",
            ib_login_key="L1",
            trading_mode="paper",
            gateway_slot=None,
            creds=Credentials("u", "p"),
            actor="op@x",
        )


@pytest.mark.asyncio
async def test_archive_delete_failure_does_not_abort_archive(broker_db_session, tmp_path):
    # finding 2: archive's secret delete is best-effort AFTER the ARCHIVED
    # commit — a delete failure must NOT propagate (would 502) and the row must
    # still be archived.
    store = _DeleteFailsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    archived = await svc.archive(acct.id, actor="op@x")  # must NOT raise
    assert archived.status == "archived"


@pytest.mark.asyncio
async def test_create_pinned_occupied_slot_raises(broker_db_session, tmp_path):
    # finding 7 (service level — needs the migration-only gateway_slot partial
    # index): pinning a slot already held by an active account raises
    # BrokerAccountError (router maps to 422). The API-level DB uses
    # create_all and lacks this index, so this conflict is asserted here.
    from msai.services.live.broker_account_service import BrokerAccountError

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a", "slot-b"])
    await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot="slot-a",
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    with pytest.raises(BrokerAccountError):
        await svc.create(
            ib_account_id="DU2",
            ib_login_key="L2",
            trading_mode="paper",
            gateway_slot="slot-a",  # already held
            creds=Credentials("u", "p"),
            actor="op@x",
        )


@pytest.mark.asyncio
async def test_update_rejects_immutable_ib_account_id(broker_db_session, tmp_path):
    # finding 7 (service level): the service's update() raises ImmutableFieldError
    # if ib_account_id is passed (router maps to 422). The router never forwards
    # it, but the service guard is the authoritative invariant.
    from msai.services.live.broker_account_service import ImmutableFieldError

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    with pytest.raises(ImmutableFieldError):
        await svc.update(acct.id, ib_account_id="DU2")


@pytest.mark.asyncio
async def test_update_label_omitted_leaves_label_unchanged(broker_db_session, tmp_path):
    # finding 2 (iter-3 P2): when the caller does NOT pass label, the existing
    # label must be preserved (the sentinel default means "field omitted").
    # iter-4 P2: a DU... (paper-prefix) account may only be set to paper — the
    # update guard rejects a switch to live (formerly this test asserted a DU
    # account could become live, codifying the prefix-vs-mode hole). Use a
    # consistent same-mode update so the label-preservation assertion is the
    # only behaviour under test here.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
        label="original",
    )
    updated = await svc.update(acct.id, trading_mode="paper")
    assert updated.label == "original"  # unchanged — label not in the call
    assert updated.trading_mode == "paper"


@pytest.mark.asyncio
async def test_update_trading_mode_mismatch_rejected(broker_db_session, tmp_path):
    # iter-4 P2: update() must enforce the SAME prefix-vs-mode guard as create,
    # else a DU/DF paper account could be PATCHed to live (nautilus gotcha #6 —
    # silent misroute). The check runs against the row's immutable ib_account_id.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU9",
        ib_login_key="L9",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    with pytest.raises(BrokerAccountError):
        await svc.update(acct.id, trading_mode="live")
    # the row is untouched after the rejection
    reread = await svc.get(acct.id)
    assert reread.trading_mode == "paper"


@pytest.mark.asyncio
async def test_update_trading_mode_consistent_succeeds(broker_db_session, tmp_path):
    # iter-4 P2: a same-mode (consistent) update still works — the guard only
    # blocks a mismatch, not a no-op or a label change alongside a valid mode.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU10",
        ib_login_key="L10",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    updated = await svc.update(acct.id, trading_mode="paper", label="renamed")
    assert updated.trading_mode == "paper"
    assert updated.label == "renamed"


@pytest.mark.asyncio
async def test_update_trading_mode_explicit_null_rejected(broker_db_session, tmp_path):
    # iter-4 P2: an explicit trading_mode=None is rejected with a clear error
    # (the column is non-nullable; a null is meaningless).
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU11",
        ib_login_key="L11",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    with pytest.raises(BrokerAccountError):
        await svc.update(acct.id, trading_mode=None)


@pytest.mark.asyncio
async def test_update_label_explicit_none_clears_label(broker_db_session, tmp_path):
    # finding 2 (iter-3 P2): an EXPLICIT label=None clears the stored label.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
        label="original",
    )
    updated = await svc.update(acct.id, label=None)
    assert updated.label is None  # explicit null clears it


@pytest.mark.asyncio
async def test_update_label_to_value_sets_label(broker_db_session, tmp_path):
    # finding 2 (iter-3 P2): a concrete label value is applied.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
        label="original",
    )
    updated = await svc.update(acct.id, label="renamed")
    assert updated.label == "renamed"


@pytest.mark.asyncio
async def test_archive_frees_slot_and_deletes_secret(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    ref, ver = acct.credentials_secret_ref, acct.credentials_secret_version
    await svc.archive(acct.id, actor="op@x")
    # slot-a is now free → a new create succeeds
    acct2 = await svc.create(
        ib_account_id="DU2",
        ib_login_key="L2",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u2", "p2"),
        actor="op@x",
    )
    assert acct2.gateway_slot == "slot-a"
    # archived account's secret is gone
    with pytest.raises(CredentialResolutionError):
        store.get(ref, ver)


@pytest.mark.asyncio
@pytest.mark.parametrize("dep_status", ["starting", "building", "ready", "running", "stopping"])
async def test_archive_blocked_while_deployment_active(broker_db_session, tmp_path, dep_status):
    # Codex iter-4 P2: archive must 409 for ANY active lifecycle state, and NOT delete the secret.
    # "stopping" is active too — a node mid-teardown still holds IB positions (nautilus gotcha #13).
    import secrets

    from msai.models.live_deployment import LiveDeployment

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    # ARRANGE a matching active deployment (direct insert — this is service-level
    # setup, not the action under test). Supply every NOT NULL column on
    # LiveDeployment, including the NOT NULL FK to a portfolio revision.
    revision = await _seed_portfolio_revision(broker_db_session)
    slug = secrets.token_hex(8)
    broker_db_session.add(
        LiveDeployment(
            account_id="DU1",
            ib_login_key="L1",
            status=dep_status,
            deployment_slug=slug,
            identity_signature=secrets.token_hex(32),
            trader_id=f"MSAI-{slug}",
            strategy_id_full=f"EMACrossStrategy-{slug}",
            portfolio_revision_id=revision.id,
            message_bus_stream=f"trader-MSAI-{slug}-stream",
        )
    )
    await broker_db_session.commit()
    with pytest.raises(AccountInUseError):
        await svc.archive(acct.id, actor="op@x")
    # secret NOT deleted — archive aborted before the store.delete
    assert store.get(acct.credentials_secret_ref, acct.credentials_secret_version) == Credentials(
        "u", "p"
    )
    # and the account is still active
    refreshed = await svc.get(acct.id)
    assert refreshed.status == "active"


@pytest.mark.asyncio
async def test_archive_blocked_by_deployment_under_different_login_key(broker_db_session, tmp_path):
    # archive-too-narrow: a deployment for the SAME ib_account_id but a
    # DIFFERENT ib_login_key (legacy "default" vs new "lvp") still holds
    # positions and must block archive. The guard matches by account_id ALONE.
    import secrets

    from msai.models.live_deployment import LiveDeployment

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="lvp",  # account registered under the NEW login key
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    revision = await _seed_portfolio_revision(broker_db_session)
    slug = secrets.token_hex(8)
    broker_db_session.add(
        LiveDeployment(
            account_id="DU1",
            ib_login_key="default",  # deployment under a DIFFERENT login key
            status="running",
            deployment_slug=slug,
            identity_signature=secrets.token_hex(32),
            trader_id=f"MSAI-{slug}",
            strategy_id_full=f"EMACrossStrategy-{slug}",
            portfolio_revision_id=revision.id,
            message_bus_stream=f"trader-MSAI-{slug}-stream",
        )
    )
    await broker_db_session.commit()
    with pytest.raises(AccountInUseError):
        await svc.archive(acct.id, actor="op@x")
    refreshed = await svc.get(acct.id)
    assert refreshed.status == "active"


@pytest.mark.asyncio
async def test_rotate_legacy_env_promotes_to_managed_backend(broker_db_session, tmp_path):
    # legacy_env rotation must PROMOTE the row to the managed backend (ENV in
    # dev): write fresh creds to broker-cred-<id>, stamp backend/ref/version,
    # and resolve_for_spawn then returns the new creds via the store.
    from msai.models.broker_account import CredentialsBackend

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")

    rotated = await svc.rotate(account_id, creds=Credentials("newu", "newp"), actor="op@x")

    assert rotated.credentials_backend == CredentialsBackend.ENV
    # unique-per-attempt promotion ref (Codex final-review P2): broker-cred-<id>-<hex>,
    # never the bare deterministic name (which a rollback would soft-delete + reserve).
    assert rotated.credentials_secret_ref.startswith(f"broker-cred-{account_id}-")
    assert rotated.credentials_secret_ref != f"broker-cred-{account_id}"
    assert rotated.credentials_secret_version is not None
    # the new creds are readable from the store at the pinned version ...
    assert store.get(
        rotated.credentials_secret_ref, rotated.credentials_secret_version
    ) == Credentials("newu", "newp")
    # ... and resolve_for_spawn now reads via the store (no longer the env path).
    resolved = await svc.resolve_for_spawn(account_id)
    assert resolved == Credentials("newu", "newp")


@pytest.mark.asyncio
async def test_resolve_for_spawn_stamps_last_accessed_and_returns_creds(
    broker_db_session, tmp_path
):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    creds = await svc.resolve_for_spawn(acct.id)
    assert creds == Credentials("u", "p")
    refreshed = await svc.get(acct.id)
    assert refreshed.credentials_last_accessed is not None


@pytest.mark.asyncio
async def test_create_persists_created_by(broker_db_session, tmp_path):
    # finding 1: the resolved operator user id is persisted to the created_by
    # audit column (previously discarded → always NULL).
    user_id = await _seed_user(broker_db_session)
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
        created_by=user_id,
    )
    refreshed = await svc.get(acct.id)
    assert refreshed.created_by == user_id


@pytest.mark.asyncio
async def test_update_on_archived_account_raises(broker_db_session, tmp_path):
    # finding 3: editing an archived account is rejected (plan US-003 edge case).
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    await svc.archive(acct.id, actor="op@x")
    with pytest.raises(AccountArchivedError):
        await svc.update(acct.id, label="renamed")


@pytest.mark.asyncio
async def test_rotate_on_archived_account_raises(broker_db_session, tmp_path):
    # finding 3: rotating an archived account is rejected.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(
        ib_account_id="DU1",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
    )
    await svc.archive(acct.id, actor="op@x")
    with pytest.raises(AccountArchivedError):
        await svc.rotate(acct.id, creds=Credentials("nu", "np"), actor="op@x")


@pytest.mark.asyncio
async def test_resolve_for_spawn_on_archived_legacy_env_raises_no_creds_no_stamp(
    broker_db_session, monkeypatch, tmp_path
):
    # finding 4: an archived legacy_env account must not yield usable creds even
    # though the env secret still exists, and must not stamp last_accessed.
    monkeypatch.setenv("TWS_USERID", "legacyuser")
    monkeypatch.setenv("TWS_PASSWORD", "legacypass")
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")
    await svc.archive(account_id, actor="op@x")  # legacy_env archive does not touch env secret
    with pytest.raises(AccountArchivedError):
        await svc.resolve_for_spawn(account_id)
    refreshed = await svc.get(account_id)
    assert refreshed.credentials_last_accessed is None  # not stamped


class _CommitFailsSession:
    """Wraps an AsyncSession so the next commit() raises, to exercise the
    rotate() legacy→managed promotion-orphan cleanup path."""

    def __init__(self, inner):
        self._inner = inner
        self._fail_next_commit = False

    def arm(self) -> None:
        self._fail_next_commit = True

    async def commit(self):
        if self._fail_next_commit:
            self._fail_next_commit = False
            raise RuntimeError("simulated commit failure")
        return await self._inner.commit()

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_rotate_promotion_commit_failure_deletes_promoted_secret(broker_db_session, tmp_path):
    # finding 6: if the commit fails on the legacy→managed promotion branch, the
    # just-written broker-cred-<id> secret must be deleted (no orphan) and the
    # row must stay legacy_env.
    from msai.models.broker_account import CredentialsBackend

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")

    wrapper = _CommitFailsSession(broker_db_session)
    svc = BrokerAccountService(db=wrapper, store=store, slots=["ib-gateway"])  # type: ignore[arg-type]
    wrapper.arm()
    with pytest.raises(RuntimeError):
        await svc.rotate(account_id, creds=Credentials("newu", "newp"), actor="op@x")

    # The promotion writes a UNIQUE broker-cred-<id>-<hex> secret then, on commit
    # failure, deletes it — so NO promotion secret is orphaned in the store
    # (Codex final-review P2: unique-per-attempt ref keeps the retry safe; we assert
    # the store has no leftover broker-cred-<id>* key).
    import json

    store_data = json.loads((tmp_path / "c.json").read_text() or "{}")
    assert not [k for k in store_data if k.startswith(f"broker-cred-{account_id}")]
    # and the row is still legacy_env (rollback discarded the in-memory mutation)
    refreshed = await BrokerAccountService(
        db=broker_db_session, store=store, slots=["ib-gateway"]
    ).get(account_id)
    assert refreshed.credentials_backend == CredentialsBackend.LEGACY_ENV
    assert refreshed.credentials_secret_ref == "env:TWS_USERID|TWS_PASSWORD"


class _RefreshFailsAfterCommitSession:
    """Wraps an AsyncSession so the FIRST refresh() after the next commit() raises.

    Exercises finding 1: a transient post-commit refresh() failure must NOT
    delete the secret the now-durable row references. The commit goes through
    the real inner session (the row is durable); the next refresh() raises.
    """

    def __init__(self, inner):
        self._inner = inner
        self._fail_next_refresh = False

    async def commit(self):
        result = await self._inner.commit()
        # Arm only after a real, successful commit so the row is durable.
        self._fail_next_refresh = True
        return result

    async def refresh(self, instance, *args, **kwargs):
        if self._fail_next_refresh:
            self._fail_next_refresh = False
            raise RuntimeError("simulated post-commit refresh failure")
        return await self._inner.refresh(instance, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_create_refresh_failure_after_commit_keeps_row_and_secret(
    broker_db_session, tmp_path
):
    # finding 1: if commit() SUCCEEDS but the post-commit refresh() raises (a
    # transient DB blip), the row is already durable and the secret it references
    # MUST survive — the orphan-secret cleanup must cover ONLY the pre-commit
    # region, never a post-commit refresh.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    wrapper = _RefreshFailsAfterCommitSession(broker_db_session)
    svc = BrokerAccountService(db=wrapper, store=store, slots=["slot-a"])  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await svc.create(
            ib_account_id="DU1",
            ib_login_key="L1",
            trading_mode="paper",
            gateway_slot=None,
            creds=Credentials("u", "p"),
            actor="op@x",
        )
    # The row is durable: a fresh service reads it back (committed before the
    # refresh blew up). And the secret it points at is still resolvable — NOT
    # deleted by a misplaced cleanup.
    fresh = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    accts = await fresh.list()
    assert len(accts) == 1
    persisted = accts[0]
    assert persisted.ib_account_id == "DU1"
    assert store.get(
        persisted.credentials_secret_ref, persisted.credentials_secret_version
    ) == Credentials("u", "p")


class _ForeignKeyIntegrityErrorSession:
    """Wraps an AsyncSession so the next commit() raises an IntegrityError whose
    constraint name is NEITHER the ib_account_id NOR the gateway_slot partial
    unique index — simulating e.g. a stale created_by FK violation.

    Exercises finding 2: such an error must be re-raised UNCHANGED (not mislabeled
    as a slot conflict / no-free-slot), and the pre-commit secret must be cleaned.
    """

    def __init__(self, inner, *, constraint_name: str):
        self._inner = inner
        self._constraint_name = constraint_name
        self._fail_next_commit = False

    def arm(self) -> None:
        self._fail_next_commit = True

    async def commit(self):
        if self._fail_next_commit:
            self._fail_next_commit = False
            await self._inner.rollback()
            orig = _FakeUniqueViolationError(self._constraint_name)
            raise IntegrityError("INSERT ...", params=None, orig=orig)
        return await self._inner.commit()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _FakeUniqueViolationError(Exception):
    """Mimics asyncpg's *Violation error carrying a ``constraint_name`` attr."""

    def __init__(self, constraint_name: str):
        super().__init__(f"violates constraint {constraint_name}")
        self.constraint_name = constraint_name


@pytest.mark.asyncio
async def test_create_unrelated_integrity_error_reraised_and_secret_cleaned(
    broker_db_session, tmp_path
):
    # finding 2: an IntegrityError on a constraint that is NEITHER the
    # ib_account_id NOR the gateway_slot index (e.g. a created_by FK violation)
    # must be re-raised UNCHANGED — not retried into a misleading NoFreeSlotError,
    # not mislabeled "slot in use". The pre-commit secret is best-effort deleted.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    wrapper = _ForeignKeyIntegrityErrorSession(
        broker_db_session, constraint_name="fk_broker_accounts_created_by_users"
    )
    svc = BrokerAccountService(db=wrapper, store=store, slots=["slot-a", "slot-b"])  # type: ignore[arg-type]
    wrapper.arm()
    with pytest.raises(IntegrityError) as ei:
        await svc.create(
            ib_account_id="DU1",
            ib_login_key="L1",
            trading_mode="paper",
            gateway_slot=None,
            creds=Credentials("u", "p"),
            actor="op@x",
        )
    # The ORIGINAL IntegrityError surfaced — not NoFreeSlotError / BrokerAccountError.
    assert not isinstance(ei.value, NoFreeSlotError)
    assert not isinstance(ei.value, BrokerAccountError)
    assert "fk_broker_accounts_created_by_users" in str(ei.value.orig)
    # No row persisted, and the pre-commit secret was best-effort deleted: the
    # JSON store file holds no secret entries.
    fresh = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    assert await fresh.list() == []
    import json

    store_path = tmp_path / "c.json"
    on_disk = json.loads(store_path.read_text()) if store_path.exists() else {}
    assert on_disk == {}  # the orphan secret was deleted


@pytest.mark.asyncio
async def test_create_pinned_unrelated_integrity_error_reraised(broker_db_session, tmp_path):
    # finding 2 (pinned variant): with a pinned slot, an unrelated IntegrityError
    # must NOT be mislabeled "gateway slot is in use" — it re-raises unchanged.
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    wrapper = _ForeignKeyIntegrityErrorSession(
        broker_db_session, constraint_name="fk_broker_accounts_created_by_users"
    )
    svc = BrokerAccountService(db=wrapper, store=store, slots=["slot-a", "slot-b"])  # type: ignore[arg-type]
    wrapper.arm()
    with pytest.raises(IntegrityError) as ei:
        await svc.create(
            ib_account_id="DU1",
            ib_login_key="L1",
            trading_mode="paper",
            gateway_slot="slot-a",
            creds=Credentials("u", "p"),
            actor="op@x",
        )
    assert not isinstance(ei.value, BrokerAccountError)
    assert "fk_broker_accounts_created_by_users" in str(ei.value.orig)


async def _seed_user(session):
    """Insert a User row (ARRANGE) and return its id (UUID)."""
    import secrets

    from msai.models.user import User

    user = User(
        entra_id=f"entra-{secrets.token_hex(8)}",
        email=f"{secrets.token_hex(6)}@test.local",
        display_name="Op",
        role="operator",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


async def _flush_uncommitted_user(session):
    """Add + FLUSH (but NOT commit) a User row, mirroring resolve_user_id's
    first-time-user behaviour: the router calls resolve_user_id(db, claims),
    which INSERTs + flushes a fresh users row in the SAME session (inside a
    SAVEPOINT) without committing, then passes that id as ``created_by`` to
    BrokerAccountService.create(). Returns the pending user's id (UUID).
    """
    import secrets

    from msai.models.user import User

    user = User(
        entra_id=f"entra-{secrets.token_hex(8)}",
        email=f"{secrets.token_hex(6)}@test.local",
        display_name="First-Time Op",
        role="operator",
    )
    async with session.begin_nested():
        session.add(user)
        await session.flush()
    return user.id


class _SlotConflictOnceSession:
    """Wraps an AsyncSession so the FIRST attempt to PERSIST a broker_account
    fails with the gateway_slot partial-unique IntegrityError, then passes
    through on the retry.

    This faithfully reproduces the multi-slot race the bug is about WITHOUT
    needing two real concurrent sessions: the first slot allocation loses the
    race (its INSERT violates ``uq_broker_accounts_active_gateway_slot``), so
    the loop must roll back ONLY that attempt (SAVEPOINT) and retry the next
    free slot. A correct implementation flushes inside ``begin_nested()`` so
    this rollback does NOT discard the pending (uncommitted) ``users`` row that
    ``created_by`` references; the buggy implementation called the session-wide
    ``rollback()`` and lost it, so the retry's INSERT failed with an FK
    violation.

    Both the persistence entrypoints the loop might use are hooked so this
    test exercises the bug against BOTH the (buggy) commit-per-attempt shape
    AND the (fixed) flush-inside-SAVEPOINT shape: the first ``flush()`` OR
    ``commit()`` that happens while a NEW BrokerAccount is pending raises the
    simulated slot conflict, then the wrapper is disarmed and everything else
    (the test's pending-user flush, the eventual outer commit) is unaffected.
    """

    def __init__(self, inner):
        self._inner = inner
        self._armed = True

    def _raise_slot_conflict_if_armed(self) -> None:
        from msai.models.broker_account import BrokerAccount

        if self._armed and any(isinstance(obj, BrokerAccount) for obj in self._inner.new):
            self._armed = False
            orig = _FakeUniqueViolationError(_GATEWAY_SLOT_CONSTRAINT)
            raise IntegrityError("INSERT broker_accounts ...", params=None, orig=orig)

    async def flush(self, *args, **kwargs):
        self._raise_slot_conflict_if_armed()
        return await self._inner.flush(*args, **kwargs)

    async def commit(self, *args, **kwargs):
        self._raise_slot_conflict_if_armed()
        return await self._inner.commit(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_create_slot_race_preserves_pending_created_by_user(broker_db_session, tmp_path):
    # transaction-isolation bug (Codex review): for a FIRST-TIME user the router
    # flushes (uncommitted) a users row in the SAME session, then passes its id as
    # created_by to create(). A slot-allocation rollback inside the retry loop used
    # to roll back the ENTIRE session transaction — discarding the pending users
    # row — so the retried INSERT pointed created_by at a user that no longer
    # existed → FK IntegrityError / 500 instead of success on the next free slot.
    #
    # Fix: each insert attempt is isolated in a SAVEPOINT (begin_nested + flush);
    # a slot-race rollback discards only the broker_account attempt, leaving the
    # pending users row + outer transaction intact, then ONE outer commit persists
    # BOTH durably.
    from msai.models.user import User

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")

    # A freshly-flushed-but-uncommitted operator user (the first-time-user path):
    # exactly what resolve_user_id leaves in the session before create() runs.
    pending_user_id = await _flush_uncommitted_user(broker_db_session)

    # Wrap the session so the FIRST broker_account flush loses the slot race,
    # forcing the SAVEPOINT-rollback-then-retry path while the pending user is
    # in the session.
    wrapper = _SlotConflictOnceSession(broker_db_session)
    svc = BrokerAccountService(db=wrapper, store=store, slots=["slot-a", "slot-b"])  # type: ignore[arg-type]

    # ACT: create an UNPINNED account. Attempt 1 → simulated slot conflict →
    # SAVEPOINT rollback (must NOT discard the pending user) → retry → success.
    acct = await svc.create(
        ib_account_id="DU-new",
        ib_login_key="L1",
        trading_mode="paper",
        gateway_slot=None,
        creds=Credentials("u", "p"),
        actor="op@x",
        created_by=pending_user_id,
    )

    # ASSERT: create succeeded on a free slot, with created_by stamped — NOT a
    # 500 from an FK violation against a discarded user.
    assert acct.gateway_slot in ("slot-a", "slot-b")
    assert acct.created_by == pending_user_id

    # ASSERT FK survival: re-read via a FRESH service and confirm created_by
    # references a users row that actually exists (committed, FK valid).
    fresh = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a", "slot-b"])
    reread = await fresh.get(acct.id)
    assert reread.created_by == pending_user_id
    user_row = await broker_db_session.get(User, pending_user_id)
    assert user_row is not None  # the pending user was committed, not discarded


async def _insert_legacy_row(session, ref: str):
    """Insert a legacy_env BrokerAccount row directly (ARRANGE) and return its id (UUID)."""
    from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend

    acct = BrokerAccount(
        ib_account_id="U4705114",
        ib_login_key="lvp",
        status=BrokerAccountStatus.ACTIVE,
        gateway_slot="ib-gateway",
        trading_mode="paper",
        credentials_backend=CredentialsBackend.LEGACY_ENV,
        credentials_secret_ref=ref,
        credentials_secret_version=None,
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct.id


@pytest.mark.asyncio
async def test_resolve_for_spawn_legacy_env_reads_paired_keys(
    broker_db_session, monkeypatch, tmp_path
):
    # legacy_env row resolves BOTH paired env keys (Codex iter-1 P1#7)
    monkeypatch.setenv("TWS_USERID", "legacyuser")
    monkeypatch.setenv("TWS_PASSWORD", "legacypass")
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")
    creds = await svc.resolve_for_spawn(account_id)
    assert creds == Credentials("legacyuser", "legacypass")


@pytest.mark.asyncio
async def test_resolve_for_spawn_legacy_env_missing_material_fails_loud(
    broker_db_session, monkeypatch, tmp_path
):
    monkeypatch.delenv("TWS_PASSWORD", raising=False)
    monkeypatch.setenv("TWS_USERID", "legacyuser")

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")
    with pytest.raises(CredentialResolutionError) as ei:
        await svc.resolve_for_spawn(account_id)
    assert ei.value.reason == KvFailureReason.NOT_FOUND


@pytest.mark.asyncio
async def test_resolve_for_spawn_rejects_null_version_for_non_legacy(broker_db_session, tmp_path):
    # an env-backed row with NULL version must fail closed (Codex iter-1 P1#4 / iter-2 P2)
    from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend

    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    acct = BrokerAccount(
        ib_account_id="DU9",
        ib_login_key="L9",
        status=BrokerAccountStatus.ACTIVE,
        gateway_slot="ib-gateway",
        trading_mode="paper",
        credentials_backend=CredentialsBackend.ENV,
        credentials_secret_ref="broker-cred-x",
        credentials_secret_version=None,  # invalid for a non-legacy row
    )
    broker_db_session.add(acct)
    await broker_db_session.commit()
    await broker_db_session.refresh(acct)
    with pytest.raises(CredentialResolutionError) as ei:
        await svc.resolve_for_spawn(acct.id)
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED


async def _seed_portfolio_revision(session):
    """Insert the minimal LivePortfolio → LivePortfolioRevision chain so a
    LiveDeployment's NOT NULL ``portfolio_revision_id`` FK resolves.
    """
    from uuid import uuid4

    from msai.models.live_portfolio import LivePortfolio
    from msai.models.live_portfolio_revision import LivePortfolioRevision

    live_portfolio = LivePortfolio(
        id=uuid4(),
        name=f"LP-{uuid4().hex[:8]}",
    )
    session.add(live_portfolio)
    await session.flush()

    revision = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=live_portfolio.id,
        revision_number=1,
        composition_hash=uuid4().hex,
        is_frozen=True,
    )
    session.add(revision)
    await session.flush()
    return revision
