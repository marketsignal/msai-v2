"""live_deployments stable identity (Phase 1 task 1.1b)

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-04-07 13:00:00.000000

Adds the v9 stable-identity columns to ``live_deployments`` so a deployment
becomes a STABLE logical record keyed by ``identity_signature`` (decision
#7). Replaces the v0 ``started_at`` / ``stopped_at`` columns with
``last_started_at`` / ``last_stopped_at`` (a deployment can be re-started
many times under the same logical identity, so single first-start /
first-stop timestamps were the wrong shape).

Backfill order is important when there are pre-existing rows:

1. Add the new columns as NULLABLE first
2. Backfill ``deployment_slug`` per row via ``secrets.token_hex(8)``
3. Backfill ``trader_id``, ``strategy_id_full``, ``message_bus_stream``
   from the slug
4. Backfill ``account_id`` from the IB env var (best-effort placeholder
   for any pre-existing rows; new rows get the real value at insert time)
5. Backfill ``instruments_signature`` by sorting + joining ``instruments``
6. Backfill ``config_hash`` from the row's ``config`` JSONB column
7. Backfill ``identity_signature`` by computing the sha256 of the canonical
   identity tuple
8. Copy ``started_at`` / ``stopped_at`` into ``last_started_at`` /
   ``last_stopped_at``
9. Tighten the new columns to NOT NULL (except the ``last_*`` and override
   columns which are intentionally nullable)
10. Add the unique indexes on ``deployment_slug`` and ``identity_signature``
11. Drop the old ``started_at`` / ``stopped_at`` columns

The downgrade reverses all of this so the migration is reversible against
a fresh DB. (Reversing against backfilled data isn't strictly safe — the
backfill loses information — but reversibility on a clean DB is enough
to keep the migration legal.)

Known limitations on legacy rows
================================

Two classes of pre-1.1b rows will cold-start ONCE on their first
post-migration restart instead of warm-reloading persisted state:

1. **Rows with ``strategy_code_hash='live'``** — the old ``/start``
   wrote a hardcoded placeholder. We deliberately do NOT recompute the
   real sha256 from disk during backfill, because the strategy file may
   have been edited since the row last ran, and assigning today's hash
   would let a warm restart load persisted state created under older
   code (Codex Task 1.1b iteration 4 P1).
2. **Rows with ``started_by IS NULL``** — the migration backfills
   ``""`` for the canonical ``started_by`` field, but the new ``/start``
   path always provisions a real ``users.id`` UUID inline. The two
   ``identity_signature`` values will not match, so the operator's first
   restart will cold-start a new row with a fresh slug. The legacy row
   stays in the DB and the operator can manually merge if they care
   (Codex Task 1.1b iteration 7 P1, deferred to Task 1.14).

Both cases are operationally identical: a single cold start with
isolated persisted state, then warm restarts work normally from that
point on. The "right" fix for case 2 — operator-driven row claiming —
needs the reservation API that Task 1.14 introduces.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Fallback account id for environments where ``ib_account_id`` is not yet
# configured (fresh installs, CI). Real deployments override this via
# ``settings.ib_account_id`` below — see :func:`_resolve_account_id`.
_FALLBACK_ACCOUNT_ID = "DU0000000"


def _resolve_account_id() -> str:
    """Pick the IB account id to bake into backfilled identity signatures.

    ``/api/v1/live/start`` derives the identity tuple from
    ``settings.ib_account_id`` at restart time. If the backfill uses a
    different value (the old hardcoded ``DU0000000``), every pre-existing
    deployment cold-starts on its first post-migration restart instead of
    warm-reloading persisted state — exactly the behavior decision #7 was
    meant to prevent (Codex Task 1.1b iteration 2, P1 fix).

    Read the same ``settings.ib_account_id`` here so backfill and /start
    agree bit-for-bit. If the setting is unconfigured (empty string or
    missing), fall back to ``DU0000000`` so fresh installs still succeed
    — those have no persisted state anyway, so the cold-start penalty
    is zero.
    """
    try:
        from msai.core.config import settings as _settings

        configured = (_settings.ib_account_id or "").strip()
        return configured or _FALLBACK_ACCOUNT_ID
    except Exception:  # noqa: BLE001
        # Settings import can fail in exotic alembic contexts (e.g. running
        # against a DB URL that doesn't match the local .env). Default to
        # the fallback so the migration still succeeds on fresh databases.
        return _FALLBACK_ACCOUNT_ID


def _normalize_legacy_config(
    request_config: dict | None,
    default_config: dict | None,
) -> dict:
    """Merge ``strategies.default_config`` underneath the row's stored config.

    ``/api/v1/live/start`` canonicalizes the request config via
    :func:`msai.services.live.deployment_identity.normalize_request_config`
    BEFORE computing ``config_hash``. The backfill must use the same
    normalization so a legacy row whose stored ``config`` omitted a
    defaulted key hashes identically to a post-migration ``/start`` call
    that also omits it (Codex Task 1.1b iteration 4, P2 fix).

    Pure-Python re-implementation — we can't import the helper here
    because alembic migrations are independent of the live application
    package graph.
    """
    req = request_config or {}
    if not default_config:
        return dict(req)
    merged = dict(default_config)
    merged.update(req)
    return merged


def _compute_identity_signature(
    *,
    started_by: object,
    strategy_id: object,
    strategy_code_hash: str,
    config: dict | None,
    paper_trading: bool,
    instruments: list[str] | None,
    account_id: str,
) -> tuple[str, str]:
    """Compute (config_hash, identity_signature) for a row.

    Extracted so the duplicate-detection pass and the backfill pass agree
    on canonicalization bit-for-bit. Matches
    :mod:`msai.services.live.deployment_identity`:

    - ``started_by=None`` → ``""`` (matches ``canonicalize_user_id``)
    - ``instruments`` are sorted + comma-joined
    - ``account_id`` is read from ``settings.ib_account_id`` by the caller
      so backfill and ``/api/v1/live/start`` agree bit-for-bit (Codex Task
      1.1b iteration 2, P1 fix)
    - both hashes use ``sort_keys + separators=(",",":")`` canonical JSON
    """
    instruments_signature = ",".join(sorted(instruments or []))
    config_dict = config or {}
    canonical_config = json.dumps(config_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    config_hash = hashlib.sha256(canonical_config).hexdigest()

    identity_dict = {
        "started_by": started_by.hex if started_by else "",  # type: ignore[attr-defined]
        "strategy_id": strategy_id.hex,  # type: ignore[attr-defined]
        "strategy_code_hash": strategy_code_hash,
        "config_hash": config_hash,
        "account_id": account_id,
        "paper_trading": paper_trading,
        "instruments_signature": instruments_signature,
    }
    canonical_identity = json.dumps(identity_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    identity_signature = hashlib.sha256(canonical_identity).hexdigest()
    return config_hash, identity_signature


def _detect_identity_collisions(connection: sa.engine.Connection) -> None:
    """Abort the upgrade if pre-existing rows would collide on identity_signature.

    Decision #7's unique index on ``identity_signature`` means any two
    pre-existing rows that canonicalize to the same signature — a real
    possibility because the old ``/start`` path inserted a fresh row per
    restart even when everything about the deployment was identical —
    would fail the ``create_index(unique=True)`` step at the end of
    ``upgrade()``. Running that ``CREATE UNIQUE INDEX`` against a
    partially-backfilled table and rolling back halfway is a lot worse
    than refusing to start.

    So we compute the signature for every row BEFORE we touch anything,
    group by signature, and fail loudly with a clear operator message
    listing the colliding row IDs. The operator is then expected to merge
    or delete the duplicates (a data decision we can't make for them)
    and re-run ``alembic upgrade head``.
    """
    # Pull strategies.default_config so we can apply the same
    # default-merge normalization /start does, so legacy rows with
    # omitted defaults hash identically to future /start calls
    # (Codex Task 1.1b iteration 4, P2 fix).
    rows = connection.execute(
        sa.text(
            """
            SELECT
                ld.id AS id,
                ld.strategy_id AS strategy_id,
                ld.strategy_code_hash AS strategy_code_hash,
                ld.config AS config,
                ld.instruments AS instruments,
                ld.paper_trading AS paper_trading,
                ld.started_by AS started_by,
                s.default_config AS strategy_default_config
            FROM live_deployments ld
            LEFT JOIN strategies s ON s.id = ld.strategy_id
            """
        )
    ).fetchall()

    account_id = _resolve_account_id()
    signatures: dict[str, list[str]] = {}
    for row in rows:
        # IMPORTANT: DO NOT re-hash the strategy file on disk here. If a
        # pre-v9 row carries the ``'live'`` placeholder and the strategy
        # file was edited after the row last ran, hashing today's bytes
        # would assign the row the *new* code hash and let a subsequent
        # /start warm-restart persisted state created under older code
        # as if it matched the current file. The historical hash is
        # unrecoverable — keep the placeholder so the first post-migration
        # restart cold-starts cleanly (Codex Task 1.1b iteration 4, P1).
        normalized_config = _normalize_legacy_config(row.config, row.strategy_default_config)
        _, sig = _compute_identity_signature(
            started_by=row.started_by,
            strategy_id=row.strategy_id,
            strategy_code_hash=row.strategy_code_hash,
            config=normalized_config,
            paper_trading=row.paper_trading,
            instruments=row.instruments,
            account_id=account_id,
        )
        signatures.setdefault(sig, []).append(str(row.id))

    collisions = {sig: ids for sig, ids in signatures.items() if len(ids) > 1}
    if not collisions:
        return

    lines = [
        "Cannot create unique index on live_deployments.identity_signature:",
        f"found {len(collisions)} colliding identity group(s) in pre-existing rows.",
        "Merge or delete duplicates before re-running `alembic upgrade head`.",
        "Colliding row IDs (one line per identity_signature):",
    ]
    for sig, ids in sorted(collisions.items()):
        lines.append(f"  {sig}: {', '.join(ids)}")
    raise RuntimeError("\n".join(lines))


def _backfill_canonical(connection: sa.engine.Connection) -> None:
    """Per-row backfill for the new identity columns.

    Implemented in Python (not pure SQL) because computing the
    canonical-JSON sha256 of the identity tuple is most readable that way
    and the migration is one-shot. Assumes :func:`_detect_identity_collisions`
    has already confirmed no pre-existing duplicates.
    """
    rows = connection.execute(
        sa.text(
            """
            SELECT
                ld.id AS id,
                ld.strategy_id AS strategy_id,
                ld.strategy_code_hash AS strategy_code_hash,
                ld.config AS config,
                ld.instruments AS instruments,
                ld.paper_trading AS paper_trading,
                ld.started_by AS started_by,
                ld.started_at AS started_at,
                ld.stopped_at AS stopped_at,
                s.strategy_class AS strategy_class,
                s.default_config AS strategy_default_config
            FROM live_deployments ld
            LEFT JOIN strategies s ON s.id = ld.strategy_id
            """
        )
    ).fetchall()

    account_id = _resolve_account_id()
    for row in rows:
        slug = secrets.token_hex(8)
        trader_id = f"MSAI-{slug}"

        strategy_class = row.strategy_class or "UnknownStrategy"
        strategy_id_full = f"{strategy_class}-{slug}"
        message_bus_stream = f"trader-{trader_id}-stream"

        # Apply the same default-config merge /start uses so legacy rows
        # with omitted defaults hash identically to future /start calls
        # (Codex Task 1.1b iteration 4, P2 fix). DO NOT touch the
        # strategy_code_hash — see comment in _detect_identity_collisions.
        normalized_config = _normalize_legacy_config(row.config, row.strategy_default_config)

        # instruments_signature: sorted, comma-joined (persisted separately
        # for diagnostics; the helper below recomputes and hashes it).
        instruments_signature = ",".join(sorted(row.instruments or []))

        config_hash, identity_signature = _compute_identity_signature(
            started_by=row.started_by,
            strategy_id=row.strategy_id,
            strategy_code_hash=row.strategy_code_hash,
            config=normalized_config,
            paper_trading=row.paper_trading,
            instruments=row.instruments,
            account_id=account_id,
        )

        connection.execute(
            sa.text(
                """
                UPDATE live_deployments SET
                    deployment_slug = :slug,
                    identity_signature = :sig,
                    trader_id = :trader_id,
                    strategy_id_full = :strategy_id_full,
                    account_id = :account_id,
                    message_bus_stream = :message_bus_stream,
                    config_hash = :config_hash,
                    instruments_signature = :instruments_signature,
                    last_started_at = :started_at,
                    last_stopped_at = :stopped_at
                WHERE id = :id
                """
            ),
            {
                "id": row.id,
                "slug": slug,
                "sig": identity_signature,
                "trader_id": trader_id,
                "strategy_id_full": strategy_id_full,
                "account_id": account_id,
                "message_bus_stream": message_bus_stream,
                "config_hash": config_hash,
                "instruments_signature": instruments_signature,
                "started_at": row.started_at,
                "stopped_at": row.stopped_at,
            },
        )


def upgrade() -> None:
    # Step 1: add new columns as NULLABLE so the backfill can run
    op.add_column(
        "live_deployments",
        sa.Column("deployment_slug", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("identity_signature", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("trader_id", sa.String(length=32), nullable=True),
    )
    # ``strategy_id_full`` is derived as ``"{strategy_class}-{slug}"``.
    # ``strategies.strategy_class`` is VARCHAR(255), slug is 16 hex chars,
    # plus the ``-`` separator → max 272 chars. Round up to 280 to leave
    # small headroom (Codex Task 1.1b iteration 3, P1 fix).
    op.add_column(
        "live_deployments",
        sa.Column("strategy_id_full", sa.String(length=280), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("account_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("message_bus_stream", sa.String(length=96), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("config_hash", sa.String(length=64), nullable=True),
    )
    # Stored as TEXT (not VARCHAR(N)) because a large options universe
    # can easily exceed any realistic fixed cap — the identity hash
    # handles uniqueness, this column is for diagnostics only (Codex
    # Task 1.1b iteration 2, P2 fix).
    op.add_column(
        "live_deployments",
        sa.Column("instruments_signature", sa.Text(), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("last_stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("startup_hard_timeout_s", sa.Integer(), nullable=True),
    )

    # Step 2: Detect pre-existing identity collisions BEFORE touching the
    # data. The old /start path inserted a fresh row per restart, so a
    # populated prod DB may have multiple rows that canonicalize to the
    # same identity_signature. Creating the unique index on such a table
    # would fail late in upgrade() after partial mutations — much worse
    # than failing fast here with a clear operator message (Codex Task
    # 1.1b P1 fix).
    connection = op.get_bind()
    _detect_identity_collisions(connection)

    # Step 3-8: Per-row backfill (Python-side because canonical-JSON sha256
    # is awkward in pure SQL)
    _backfill_canonical(connection)

    # Step 9: tighten new columns to NOT NULL (except the intentionally
    # nullable ones)
    op.alter_column("live_deployments", "deployment_slug", nullable=False)
    op.alter_column("live_deployments", "identity_signature", nullable=False)
    op.alter_column("live_deployments", "trader_id", nullable=False)
    op.alter_column("live_deployments", "strategy_id_full", nullable=False)
    op.alter_column("live_deployments", "account_id", nullable=False)
    op.alter_column("live_deployments", "message_bus_stream", nullable=False)
    op.alter_column("live_deployments", "config_hash", nullable=False)
    op.alter_column("live_deployments", "instruments_signature", nullable=False)
    # last_started_at, last_stopped_at, startup_hard_timeout_s stay NULLABLE.

    # Step 10: unique indexes on deployment_slug and identity_signature
    op.create_index(
        op.f("ix_live_deployments_deployment_slug"),
        "live_deployments",
        ["deployment_slug"],
        unique=True,
    )
    op.create_index(
        op.f("ix_live_deployments_identity_signature"),
        "live_deployments",
        ["identity_signature"],
        unique=True,
    )

    # Step 11: drop the old single-start/single-stop columns
    op.drop_column("live_deployments", "started_at")
    op.drop_column("live_deployments", "stopped_at")


def downgrade() -> None:
    # Re-add the old timestamp columns
    op.add_column(
        "live_deployments",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "live_deployments",
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Best-effort: copy the most-recent timestamps back so any backfilled
    # data is preserved when downgrading.
    op.execute(
        "UPDATE live_deployments SET started_at = last_started_at, stopped_at = last_stopped_at"
    )

    # Drop the new indexes + columns
    op.drop_index(op.f("ix_live_deployments_identity_signature"), table_name="live_deployments")
    op.drop_index(op.f("ix_live_deployments_deployment_slug"), table_name="live_deployments")

    op.drop_column("live_deployments", "startup_hard_timeout_s")
    op.drop_column("live_deployments", "last_stopped_at")
    op.drop_column("live_deployments", "last_started_at")
    op.drop_column("live_deployments", "instruments_signature")
    op.drop_column("live_deployments", "config_hash")
    op.drop_column("live_deployments", "message_bus_stream")
    op.drop_column("live_deployments", "account_id")
    op.drop_column("live_deployments", "strategy_id_full")
    op.drop_column("live_deployments", "trader_id")
    op.drop_column("live_deployments", "identity_signature")
    op.drop_column("live_deployments", "deployment_slug")
