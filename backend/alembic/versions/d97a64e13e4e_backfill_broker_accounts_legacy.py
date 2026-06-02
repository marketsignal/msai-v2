"""backfill broker_accounts legacy

Revision ID: d97a64e13e4e
Revises: d87c2aa5f751
Create Date: 2026-06-01 23:14:43.875349

PR 3 (BrokerAccount entity), Task 11: env-driven, idempotent data backfill that
seeds the ``broker_accounts`` table with the legacy per-environment accounts.

Driven entirely by the ``BROKER_ACCOUNT_BACKFILL`` env var so the same migration
seeds DIFFERENT accounts per environment (LVP on dev, HVP in prod) — the gateway
slots differ per host, so a hardcoded mapping would seed a non-existent slot.
**Empty/unset env → no rows inserted (safe no-op default).** Each environment
opts in via its own env var (mirrors the existing ``GATEWAY_CONFIG`` convention).

Format (comma-separated entries, each exactly 5 colon-parts)::

    ib_account_id:ib_login_key:gateway_slot:trading_mode:USERID_KEY|PASSWORD_KEY

Each entry seeds one ``legacy_env`` row with
``credentials_secret_ref="env:<USERID_KEY>|<PASSWORD_KEY>"`` (paired) and
``credentials_secret_version=NULL``. The migration writes ROWS only — it never
reads or moves secret material (PRD acceptance criterion).

Idempotent: any ``ib_account_id`` that already has a non-archived row is skipped.
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op
from msai.services.nautilus.ib_port_validator import assert_account_mode_consistent

# revision identifiers, used by Alembic.
revision: str = "d97a64e13e4e"
down_revision: str = "d87c2aa5f751"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    conn = op.get_bind()
    raw = os.environ.get("BROKER_ACCOUNT_BACKFILL", "").strip()
    if not raw:
        return  # safe no-op default — each environment opts in via its own env var

    existing = {
        r[0]
        for r in conn.execute(
            sa.text("SELECT ib_account_id FROM broker_accounts WHERE status <> 'archived'")
        )
    }
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        # Strip every part — a hand-edited env with stray whitespace would otherwise
        # seed a malformed active row, e.g. a padded gateway_slot ' ib-gateway ' reserves
        # a DIFFERENT slot than 'ib-gateway' (auto-allocation later re-hands the real
        # slot), or a blank ib_login_key/account-id (Codex iter-11 P2).
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 5:
            raise ValueError(f"BROKER_ACCOUNT_BACKFILL entry malformed: {entry!r}")
        ib_account_id, login, slot, mode, key_pair = parts
        if not (ib_account_id and login and slot and mode):
            raise ValueError(
                f"BROKER_ACCOUNT_BACKFILL entry has a blank field "
                f"(ib_account_id:ib_login_key:gateway_slot:trading_mode all required): {entry!r}"
            )
        # key_pair must be USERID_KEY|PASSWORD_KEY with both halves present —
        # the legacy_env resolver partitions on "|" and reads both env keys.
        # A missing/empty half would silently produce an unresolvable pointer.
        userid_key, sep, password_key = (p.strip() for p in key_pair.partition("|"))
        if not sep or not userid_key or not password_key:
            raise ValueError(
                f"BROKER_ACCOUNT_BACKFILL entry key_pair must be "
                f"USERID_KEY|PASSWORD_KEY with both halves non-empty: {entry!r}"
            )
        # Prefix-vs-mode guard (iter-5 P2-2b): reject a U-prefix (live) account
        # paired with paper mode (or a DU/DF paper account paired with live) —
        # the SAME shared helper the create/update API path uses, so the two
        # cannot drift. Pairing a live account with paper mode silently misroutes
        # orders at the gateway (nautilus gotcha #6); catch it loud at
        # `alembic upgrade` rather than at first deployment.
        try:
            assert_account_mode_consistent(ib_account_id, mode)
        except ValueError as exc:
            raise ValueError(
                f"BROKER_ACCOUNT_BACKFILL entry {entry!r} has an account/mode mismatch: {exc}"
            ) from exc
        if ib_account_id in existing:
            continue  # idempotent: skip an account already present (non-archived)
        conn.execute(
            sa.text(
                "INSERT INTO broker_accounts "
                "(id, ib_account_id, ib_login_key, status, gateway_slot, trading_mode, "
                " credentials_backend, credentials_secret_ref, credentials_secret_version) "
                "VALUES (gen_random_uuid(), :a, :l, 'active', :s, :m, 'legacy_env', :ref, NULL)"
            ),
            {
                "a": ib_account_id,
                "l": login,
                "s": slot,
                "m": mode,
                # rebuild the ref from the STRIPPED halves so a padded key_pair
                # (`env:TWS_USERID | TWS_PASSWORD`) yields a clean `env:USERID|PASSWORD`.
                "ref": f"env:{userid_key}|{password_key}",
            },
        )
        existing.add(ib_account_id)


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM broker_accounts WHERE credentials_backend = 'legacy_env'")
    )
