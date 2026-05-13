"""Identity computation for stable deployment identification.

The ``identity_signature`` is a sha256 of the canonical-JSON of the
:class:`DeploymentIdentity` dataclass — see decision #7 in the hardening
plan (Phase 1 task 1.1b).

Two ``live_deployments`` rows with the same ``identity_signature`` SHARE
state across restarts (warm reload via Nautilus's ``CacheConfig.database``
+ stable ``trader_id``). Two rows with ANY different field have different
signatures and start cold.

The identity tuple intentionally includes:

- ``started_by``           — different operators get different deployments
- ``strategy_id``          — different strategies are obviously different
- ``strategy_code_hash``   — editing the strategy file is a cold start
- ``config_hash``          — tweaking a parameter is a cold start
- ``account_id``           — switching brokers is a cold start
- ``paper_trading``        — paper vs live are independent
- ``instruments_signature`` — different instrument sets are independent

Codex v4 P0 found that v4's coarser ``(user, strategy, instruments, paper)``
tuple silently reused the same ``trader_id`` across config/code/account
changes, which would have made Nautilus reload incompatible state into a
materially different deployment. v5+ uses the broader tuple here.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(slots=True, frozen=True)
class DeploymentIdentity:
    """Everything that distinguishes one logical deployment from another.

    Two deployments with the same :meth:`signature` are the SAME deployment
    and share state across restarts. Any difference in any field produces a
    different signature → cold start with isolated state.
    """

    started_by: str
    strategy_id: str
    strategy_code_hash: str
    config_hash: str
    account_id: str
    paper_trading: bool
    instruments_signature: str

    def to_canonical_json(self) -> bytes:
        """Stable serialization for hashing.

        - ``sort_keys=True`` so dataclass field order doesn't affect output
        - ``separators=(",", ":")`` so whitespace doesn't affect output
        - UTF-8 encoded so the byte stream is reproducible across platforms
        """
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def signature(self) -> str:
        """64-char sha256 hex — the unique ``identity_signature`` for the row."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


def compute_instruments_signature(instruments: list[str]) -> str:
    """Stable, normalized signature for the instrument set.

    Sorts ascending then joins with commas. The caller is responsible for
    de-duping; this helper preserves duplicates so caller bugs aren't
    silently masked.

    Used for the identity tuple's ``instruments_signature`` field AND
    persisted on the row for diagnostics.
    """
    return ",".join(sorted(instruments))


def compute_config_hash(config: BaseModel | dict[str, Any]) -> str:
    """sha256 hex of the canonical JSON of the strategy config.

    Accepts either a Pydantic ``BaseModel`` (preferred) or a raw ``dict``.
    The Pydantic path normalizes via ``model_dump(mode="json")`` first,
    which applies type coercion and defaults — semantically-identical
    configs produce the same hash even if one was constructed with a
    string and the other with the coerced int (Codex v5 P3 fix).

    Raw-dict input is accepted as a convenience for tests and migrations
    but the production endpoint always passes a normalized dict (see
    :func:`normalize_request_config`) so omitted defaults hash identically
    to explicitly-passed defaults.
    """
    if isinstance(config, BaseModel):
        normalized: Any = config.model_dump(mode="json")
    else:
        normalized = config
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalize_request_config(
    request_config: dict[str, Any],
    strategy_default_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge the strategy's stored default config under the request dict.

    Two deployments are semantically identical when they differ only in
    whether the caller explicitly passed a parameter that happens to match
    the strategy's default. Without normalization, ``{"fast": 10}`` and
    ``{}`` (with strategy default ``fast=10``) hash differently and would
    become two separate cold-start deployments instead of warm-restarting
    the same logical one (Codex Task 1.1b P2 fix, scoped subset).

    This is the minimal normalization available without loading the
    Nautilus ``StrategyConfig`` msgspec class into the API process —
    full type coercion (``"10"`` vs ``10``) waits for Task 1.14 where
    the strategy-config pipeline lands. For now the contract is:

    - If the strategy has ``default_config``, fill in any missing keys
      from request_config with the default value.
    - Request values always win over defaults (explicit override).
    - Returns a NEW dict; inputs are not mutated.

    The result is safe to pass through :func:`compute_config_hash` for
    the canonical-JSON sha256.
    """
    if not strategy_default_config:
        return dict(request_config)
    merged = dict(strategy_default_config)
    merged.update(request_config)
    return merged


def generate_deployment_slug() -> str:
    """16 hex chars = 64 bits.

    Used as the stable per-deployment Nautilus identity (``trader_id``,
    ``order_id_tag``, ``StrategyId`` suffix). Generated ONCE at first
    creation of a logical deployment and reused across restarts (decision
    #7).

    64 bits gives a birthday-collision threshold around 4 billion
    deployments — far enough away that for a personal hedge fund this is
    effectively safe.
    """
    return secrets.token_hex(8)


def derive_trader_id(slug: str) -> str:
    """``MSAI-{slug}`` — the Nautilus ``TraderId`` value for the live node."""
    return f"MSAI-{slug}"


def derive_strategy_id_full(
    strategy_class_name: str, slug: str, order_index: int = 0,
) -> str:
    """``{class_name}-{order_index}-{slug}`` — the Nautilus ``StrategyId.value``.

    The ``order_index`` disambiguates multiple strategies of the same class
    within a portfolio deployment (e.g. two ``EMACross`` with different
    configs). For single-strategy backward compat, ``order_index`` defaults
    to ``0``.

    .. note::

       This changes the format from ``"{class}-{slug}"`` (pre-portfolio)
       to ``"{class}-{order_index}-{slug}"``. Existing callers that pass
       only 2 args get ``order_index=0``, producing a DIFFERENT string
       than the old format. Existing deployments will cold-start on first
       restart with the new code — this is acceptable because the
       identity_signature (not strategy_id_full) governs warm restart,
       and existing single-strategy deployments retain their
       identity_signature.
    """
    return f"{strategy_class_name}-{order_index}-{slug}"


def derive_message_bus_stream(slug: str) -> str:
    """``trader-MSAI-{slug}:stream`` — the deterministic Redis Stream name
    where Nautilus publishes message-bus events for this trader (Phase 3
    task 3.2 with ``stream_per_topic=False``).

    Persisted on the row at deployment-creation time so the projection
    consumer (3.4) knows what stream to ``XREADGROUP`` from without
    polling Redis for stream names.

    The separator before ``stream`` is a **colon**, matching what
    Nautilus's Rust MessageBus actually writes: with ``use_trader_prefix=True``,
    ``use_trader_id=True``, and ``streams_prefix='stream'`` (see
    ``live_node_config.py``), Nautilus constructs the name as
    ``"{prefix}-{trader_id}:{streams_prefix}"``. Bug B, 2026-04-16:
    this helper previously returned ``-stream`` (hyphen). Every
    PositionOpened / OrderFilled / AccountState event since MSAI
    wired the projection consumer was being silently dropped
    because the consumer was XREADGROUP'ing an empty
    ``...-stream`` key while Nautilus was writing to
    ``...:stream``. Confirmed on 5 deployments: hyphen-keys
    had 0 entries, colon-keys 64–2582.
    """
    return f"trader-{derive_trader_id(slug)}:stream"


def canonicalize_user_id(user_id: UUID | None, *, fallback_sub: str | None = None) -> str:
    """Canonical string form of the caller identity for the identity tuple.

    Three cases:

    - ``user_id`` resolved to a UUID → use its 32-char hex. This is the
      normal authenticated path where ``/api/v1/live/start`` found a
      ``users`` row for the JWT ``sub``.
    - ``user_id`` is ``None`` but ``fallback_sub`` is set → use the auth
      ``sub`` claim verbatim (prefixed with ``"sub:"`` so it can never
      collide with a hex UUID). This handles the v9 edge case where a
      JWT user calls ``/start`` before ``/auth/me`` has provisioned
      their ``users`` row — without the fallback, two different
      first-time users would collapse to the same anonymous identity
      and warm-restart each other's deployments (Codex Task 1.1b
      iteration 5, P1 fix).
    - Both ``None`` → ``""``. Mirrors the Alembic backfill so legacy
      anonymous rows hash identically across the migration boundary.
    """
    if user_id is not None:
        return user_id.hex
    if fallback_sub:
        return f"sub:{fallback_sub}"
    return ""


@dataclass(slots=True, frozen=True)
class PortfolioDeploymentIdentity:
    """Identity tuple for portfolio-based deployments.

    Unlike :class:`DeploymentIdentity` which identifies a single-strategy
    deployment, this identifies a *portfolio* deployment — a set of
    strategies deployed together to a single account.

    Two portfolio deployments with the same :meth:`signature` share state
    across restarts. Any field difference → cold start.
    """

    started_by: str
    portfolio_revision_id: str
    account_id: str
    paper_trading: bool
    ib_login_key: str

    def to_canonical_json(self) -> bytes:
        """Stable serialization for hashing (same contract as DeploymentIdentity)."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def signature(self) -> str:
        """64-char sha256 hex — the unique ``identity_signature`` for the row."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


def derive_portfolio_deployment_identity(
    *,
    user_id: UUID | None,
    portfolio_revision_id: UUID,
    account_id: str,
    paper_trading: bool,
    ib_login_key: str,
    user_sub: str | None = None,
) -> PortfolioDeploymentIdentity:
    """Convenience builder for :class:`PortfolioDeploymentIdentity`.

    Mirrors :func:`derive_deployment_identity` but takes portfolio-level
    inputs instead of single-strategy inputs.

    ``ib_login_key`` is part of the identity tuple: switching IB usernames
    on the same revision+account produces a new identity_signature (cold
    start), which is correct — different sessions cannot share the same
    Nautilus subprocess.
    """
    return PortfolioDeploymentIdentity(
        started_by=canonicalize_user_id(user_id, fallback_sub=user_sub),
        portfolio_revision_id=portfolio_revision_id.hex,
        account_id=account_id,
        paper_trading=paper_trading,
        ib_login_key=ib_login_key,
    )


def derive_deployment_identity(
    *,
    user_id: UUID | None,
    strategy_id: UUID,
    strategy_code_hash: str,
    config: BaseModel | dict[str, Any],
    account_id: str,
    paper_trading: bool,
    instruments: list[str],
    user_sub: str | None = None,
) -> DeploymentIdentity:
    """Convenience builder that takes the raw ``/api/v1/live/start`` inputs
    and produces a normalized :class:`DeploymentIdentity`.

    The endpoint calls this with the Pydantic-validated config model so
    semantically-identical configs hash the same (Codex v5 P3).

    ``user_id`` may be ``None`` for API-key requests OR for JWT users
    whose ``users`` row hasn't been provisioned yet. In the second case,
    the caller passes ``user_sub`` (the JWT ``sub`` claim) so the
    identity is still stable per-user (Codex Task 1.1b iteration 5, P1
    fix). In the first case, ``user_sub=None`` → identity canonicalizes
    to the same ``""`` the Alembic backfill uses.
    """
    return DeploymentIdentity(
        started_by=canonicalize_user_id(user_id, fallback_sub=user_sub),
        strategy_id=strategy_id.hex,
        strategy_code_hash=strategy_code_hash,
        config_hash=compute_config_hash(config),
        account_id=account_id,
        paper_trading=paper_trading,
        instruments_signature=compute_instruments_signature(instruments),
    )
