"""Idempotency layer for ``POST /api/v1/live/start`` (Phase 1 task 1.14).

Three tightly-linked abstractions:

1. :class:`EndpointOutcome` — a structured value the endpoint produces
   on every code path. Carries ``status_code`` + ``response`` body +
   a ``cacheable`` flag + a :class:`FailureKind` tag. The endpoint's
   final step reads ``outcome.cacheable`` and calls
   ``IdempotencyStore.commit(...)`` or ``.release(...)`` on that
   single bit. No status-code allowlists, no string parsing.

2. :class:`IdempotencyStore` — Redis-backed atomic in-flight
   reservation. ``reserve()`` uses ``SET NX EX`` to claim a key-scoped
   slot; concurrent retries get ``InFlight``. ``commit()`` rewrites the
   key with the cached outcome and a 24 h TTL. ``release()`` deletes
   the key so a later retry can re-attempt.

3. :class:`ReservationResult` union — ``reserve()`` returns exactly
   one of ``Reserved`` / ``InFlight`` / ``CachedOutcome`` /
   ``BodyMismatchReservation``. The endpoint pattern-matches on this
   result — **only the Reserved branch may call ``commit()`` or
   ``release()``** (Codex v7 P0 from plan v8). The other branches
   return their outcome immediately without touching the store.

Key format (user-scoped to eliminate cross-principal leak):
``msai:idem:start:{user_id_hex}:{sha256(idempotency_key)}``.

TTLs (plan v6, preserved in v9):
- ``RESERVATION_TTL_S = 300`` — covers the worst-case startup path
  (``build_timeout_s 120`` + ``startup_health_timeout_s 60`` +
  ``api_poll_timeout_s 60`` + margin).
- ``RESPONSE_TTL_S = 86400`` — 24 h for cacheable outcomes only.

Serialization: JSON (msgpack is not in the dependency tree). The
structure is stable enough that JSON round-trip is trivially testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from msai.services.live.failure_kind import FailureKind

if TYPE_CHECKING:
    from uuid import UUID

    from redis.asyncio import Redis as AsyncRedis


# TTLs (plan v6, unchanged in v9)
RESERVATION_TTL_S = 300
"""Worst-case startup path TTL: ``build_timeout_s (120) +
startup_health_timeout_s (60) + api_poll_timeout_s (60) + margin``."""

RESPONSE_TTL_S = 86400
"""24 hours — cacheable responses only (commit path)."""


# ---------------------------------------------------------------------------
# EndpointOutcome
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EndpointOutcome:
    """Structured endpoint outcome. Used by ``/api/v1/live/start`` to
    produce an HTTP response AND decide whether the idempotency layer
    should cache it.

    The endpoint's final step is a single branch::

        if outcome.cacheable:
            await idem.commit(redis_key, body_hash, outcome)
        else:
            await idem.release(redis_key)

    No code inspects ``status_code`` to decide cacheability — that
    decision lives in :attr:`cacheable` and is set by the factory
    that built the outcome. This is the plan v7 replacement for v6's
    status-code-based allowlist (Codex v6 P1).
    """

    status_code: int
    response: dict[str, Any]
    cacheable: bool
    failure_kind: FailureKind = FailureKind.NONE

    # --- Success / already-active factories --------------------------

    @classmethod
    def ready(cls, body: dict[str, Any]) -> EndpointOutcome:
        """Cold/warm start succeeded and the subprocess reached
        ``ready``. HTTP 201, cacheable."""
        return cls(status_code=201, response=body, cacheable=True)

    @classmethod
    def already_active(cls, body: dict[str, Any]) -> EndpointOutcome:
        """The deployment's most recent ``live_node_processes`` row is
        already in an active status. HTTP **200** (not 201) — this is
        an idempotent retry that did not spawn a new process.

        Plan v7 fix: the v6 workflow had a 200 vs. 201 mismatch between
        this factory and the idempotency store's status-code allowlist;
        v7+ returns 200 here and the store caches it.
        """
        return cls(status_code=200, response=body, cacheable=True)

    @classmethod
    def stopped(cls, body: dict[str, Any]) -> EndpointOutcome:
        """``/stop`` path: the deployment was running and is now
        stopped (or was already stopped — idempotent). HTTP 200,
        cacheable."""
        return cls(status_code=200, response=body, cacheable=True)

    # --- Transient / non-cacheable factories -------------------------

    @classmethod
    def halt_active(cls) -> EndpointOutcome:
        """The ``msai:risk:halt`` Redis flag was set. HTTP 503,
        **not cacheable** — a subsequent retry after ``/resume``
        clears the flag should be allowed to re-attempt."""
        return cls(
            status_code=503,
            response={
                "detail": ("Kill switch is active. POST /api/v1/live/resume to clear."),
            },
            cacheable=False,
            failure_kind=FailureKind.HALT_ACTIVE,
        )

    @classmethod
    def in_flight(cls) -> EndpointOutcome:
        """Another request with the same Idempotency-Key is currently
        holding the reservation. HTTP 425 Too Early, not cacheable."""
        return cls(
            status_code=425,
            response={
                "detail": ("Another request with the same Idempotency-Key is in flight."),
            },
            cacheable=False,
            failure_kind=FailureKind.IN_FLIGHT,
        )

    @classmethod
    def api_poll_timeout(cls) -> EndpointOutcome:
        """The endpoint waited the full ``api_poll_timeout_s`` for the
        subprocess to reach ``ready``/``failed`` and neither happened.
        HTTP 504, not cacheable — the next retry can re-attempt."""
        return cls(
            status_code=504,
            response={
                "detail": ("Deployment did not reach 'ready' within the poll timeout."),
            },
            cacheable=False,
            failure_kind=FailureKind.API_POLL_TIMEOUT,
        )

    @classmethod
    def flatness_unknown(
        cls,
        *,
        deployment_id: str,
        stop_nonce: str,
        process_status: str,
    ) -> EndpointOutcome:
        """Supervisor confirmed terminal but the child never wrote a
        flatness report (`stop_report:{nonce}` never materialized).

        Bug #2 protocol per `docs/decisions/redis-flatness-protocol.md`
        "broker_flat: unknown" — the deployment IS stopped (DB row is
        terminal), but the wire that verifies broker flatness never
        confirmed. Operator MUST verify positions via IB portal.

        HTTP 504, not cacheable — the deployment is stopped, but the
        flatness verification mechanism failed; subsequent retries
        won't recover this signal (the process is already gone). The
        504 forces the client to acknowledge the unknown state rather
        than silently surfacing `broker_flat: null` as success."""
        return cls(
            status_code=504,
            response={
                "id": deployment_id,
                "status": "stopped",
                "process_status": process_status,
                "stop_nonce": stop_nonce,
                "broker_flat": None,
                "remaining_positions": [],
                "detail": {
                    "error": {
                        "code": "FLATNESS_UNKNOWN",
                        "message": (
                            "Deployment stopped but the child never wrote a "
                            "flatness report. Operator must verify broker "
                            "positions via IB portal."
                        ),
                        "details": {
                            "hint": (
                                f"check stop_report:{stop_nonce} in Redis manually if "
                                "needed; the 120s TTL on the key may still hold the "
                                "report if it arrived late."
                            ),
                        },
                    },
                },
            },
            cacheable=False,
            failure_kind=FailureKind.API_POLL_TIMEOUT,
        )

    @classmethod
    def spawn_failed_transient(cls, error_message: str) -> EndpointOutcome:
        """The supervisor's payload factory raised a transient error
        (Postgres briefly down, network timeout during module import,
        etc.). HTTP 503, **not cacheable** — Codex iter6 P2 regression.
        The command stays in the Redis command bus PEL for XAUTOCLAIM
        redelivery once the dependency recovers, so retrying with the
        same Idempotency-Key should be allowed to re-attempt. Caching
        this response would turn a recoverable blip into a terminal
        failure that clients see forever until the key expires.
        """
        return cls(
            status_code=503,
            response={
                "detail": f"Transient supervisor error, please retry: {error_message}",
                "failure_kind": FailureKind.SPAWN_FAILED_TRANSIENT.value,
            },
            cacheable=False,
            failure_kind=FailureKind.SPAWN_FAILED_TRANSIENT,
        )

    @classmethod
    def body_mismatch(cls) -> EndpointOutcome:
        """Same ``Idempotency-Key`` reused with a different request
        body. HTTP 422, **not cacheable** (Codex v7 P0 — a
        body-mismatch caller does NOT own the reservation slot, so
        caching this 422 would overwrite the original correct cached
        response at the same key)."""
        return cls(
            status_code=422,
            response={
                "detail": "Idempotency-Key reused with a different request body.",
            },
            cacheable=False,
            failure_kind=FailureKind.BODY_MISMATCH,
        )

    # --- Permanent-failure factory (cacheable) -----------------------

    @classmethod
    def permanent_failure(
        cls,
        row_failure_kind: FailureKind,
        error_message: str,
    ) -> EndpointOutcome:
        """Build a cacheable HTTP 503 from a DB row's
        ``failure_kind``. Accepts ``SPAWN_FAILED_PERMANENT``,
        ``RECONCILIATION_FAILED``, ``BUILD_TIMEOUT``, and
        ``UNKNOWN`` — the last one comes from
        :meth:`FailureKind.parse_or_unknown` when the column holds a
        NULL, stale, or corrupted value. The endpoint treats
        UNKNOWN as a permanent failure (cacheable) with the
        human-readable ``error_message`` so the operator can
        investigate; retries with the same Idempotency-Key return
        the CACHED 503 without re-attempting.
        """
        assert row_failure_kind in _PERMANENT_FAILURE_KINDS, (
            f"permanent_failure called with non-permanent kind: {row_failure_kind!r}"
        )
        return cls(
            status_code=503,
            response={
                "detail": error_message,
                "failure_kind": row_failure_kind.value,
            },
            cacheable=True,
            failure_kind=row_failure_kind,
        )

    @classmethod
    def registry_permanent_failure(
        cls,
        row_failure_kind: FailureKind,
        error_message: str,
    ) -> EndpointOutcome:
        """Build an HTTP 422 from a DB row for the four registry-class
        ``failure_kind`` values. Accepts ``REGISTRY_MISS``,
        ``REGISTRY_INCOMPLETE``, ``UNSUPPORTED_ASSET_CLASS``, and
        ``AMBIGUOUS_REGISTRY``.

        ``error_message`` is expected to be a JSON-encoded envelope
        produced by ``LiveResolverError.to_error_message()``:
        ``{"code": "...", "message": "...", "details": {...}}``.
        On parse failure (legacy row, hand-edited column), the whole
        string becomes the ``message`` and ``details`` is ``{}``.

        Response envelope follows ``.claude/rules/api-design.md``:
        ``{"error": {"code": str, "message": str, "details": dict},
           "failure_kind": str}``.

        Cacheable=False because registry failures are OPERATOR-CORRECTABLE
        (run ``msai instruments refresh`` / close alias / pick supported
        asset class) — caching the 422 would block retry-after-fix with
        the same Idempotency-Key. Unlike permanent_failure (503 /
        build-timeout / reconciliation), retrying after operator
        correction is the expected recovery path.
        """
        assert row_failure_kind in _REGISTRY_FAILURE_KINDS, (
            f"registry_permanent_failure called with non-registry kind: {row_failure_kind!r}"
        )

        try:
            parsed: Any = json.loads(error_message)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}

        code = parsed.get("code") or row_failure_kind.value.upper()
        message = parsed.get("message") or error_message
        details_raw = parsed.get("details")
        details = details_raw if isinstance(details_raw, dict) else {}

        return cls(
            status_code=422,
            response={
                "error": {
                    "code": code,
                    "message": message,
                    "details": details,
                },
                "failure_kind": row_failure_kind.value,
            },
            cacheable=False,
            failure_kind=row_failure_kind,
        )


PERMANENT_FAILURE_KINDS: frozenset[FailureKind] = frozenset(
    {
        FailureKind.SPAWN_FAILED_PERMANENT,
        FailureKind.RECONCILIATION_FAILED,
        FailureKind.BUILD_TIMEOUT,
        FailureKind.HEARTBEAT_TIMEOUT,
        FailureKind.REGISTRY_MISS,
        FailureKind.REGISTRY_INCOMPLETE,
        FailureKind.UNSUPPORTED_ASSET_CLASS,
        FailureKind.AMBIGUOUS_REGISTRY,
        FailureKind.UNKNOWN,
    }
)
# Backward compat: leading-underscore alias retained for callers that
# imported the private name before the public promotion.
_PERMANENT_FAILURE_KINDS = PERMANENT_FAILURE_KINDS


REGISTRY_FAILURE_KINDS: frozenset[FailureKind] = frozenset(
    {
        FailureKind.REGISTRY_MISS,
        FailureKind.REGISTRY_INCOMPLETE,
        FailureKind.UNSUPPORTED_ASSET_CLASS,
        FailureKind.AMBIGUOUS_REGISTRY,
    }
)
_REGISTRY_FAILURE_KINDS = REGISTRY_FAILURE_KINDS  # backward compat


# ---------------------------------------------------------------------------
# ReservationResult union
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Reserved:
    """``SET NX`` won the race — the caller owns the slot.

    The endpoint MUST eventually call either
    :meth:`IdempotencyStore.commit` (for cacheable outcomes) or
    :meth:`IdempotencyStore.release` (for transient outcomes and
    raised exceptions) using ``redis_key``.
    """

    redis_key: str


@dataclass(slots=True, frozen=True)
class InFlight:
    """Another request with the same key is in flight. The endpoint
    returns :meth:`EndpointOutcome.in_flight` and MUST NOT touch the
    store."""


@dataclass(slots=True, frozen=True)
class CachedOutcome:
    """A prior request with the same key completed with a cacheable
    outcome. The endpoint returns ``outcome`` unchanged and MUST NOT
    touch the store."""

    outcome: EndpointOutcome


@dataclass(slots=True, frozen=True)
class BodyMismatchReservation:
    """A prior request with the same key completed (or is still in
    flight) with a DIFFERENT body hash. The endpoint returns
    :meth:`EndpointOutcome.body_mismatch` and MUST NOT touch the
    store — caching the 422 would overwrite the original correct
    cached response."""


ReservationResult = Reserved | InFlight | CachedOutcome | BodyMismatchReservation


# ---------------------------------------------------------------------------
# IdempotencyStore
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Redis-backed Idempotency-Key store with atomic in-flight
    reservation.

    Key format: ``msai:idem:start:{user_id_hex}:{sha256(key)}`` —
    user-scoped to eliminate cross-principal leak (Codex v4 P2).

    States:

    - **Missing** — no prior request with this key → ``SET NX`` succeeds
    - **PENDING** — another request is in flight → ``reserve()``
      returns ``InFlight``
    - **Completed cacheable** — serialized ``EndpointOutcome`` →
      ``reserve()`` returns ``CachedOutcome`` (or
      ``BodyMismatchReservation`` if the body differs)

    The endpoint code path is a single match::

        match await idem.reserve(user_id, key, body_hash):
            case Reserved(redis_key=k):
                try:
                    outcome = await build_outcome(...)
                except Exception:
                    await idem.release(k)
                    raise
                if outcome.cacheable:
                    await idem.commit(k, body_hash, outcome)
                else:
                    await idem.release(k)
                return outcome
            case CachedOutcome(outcome=cached):
                return cached
            case InFlight():
                return EndpointOutcome.in_flight()
            case BodyMismatchReservation():
                return EndpointOutcome.body_mismatch()
    """

    def __init__(self, redis: AsyncRedis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_key(user_id: UUID, key: str) -> str:
        """User-scoped Redis key. The ``sha256`` hash of the raw key
        keeps the Redis key length bounded regardless of how long the
        client sends ``Idempotency-Key``."""
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"msai:idem:start:{user_id.hex}:{h}"

    @staticmethod
    def body_hash(body: dict[str, Any]) -> str:
        """Compute a stable body hash for mismatch detection. Uses
        canonical-JSON (sorted keys) so semantically-identical bodies
        produce the same hash regardless of Python dict ordering."""
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Reserve
    # ------------------------------------------------------------------

    async def reserve(
        self,
        *,
        user_id: UUID,
        key: str,
        body_hash: str,
    ) -> ReservationResult:
        """Atomic ``SET NX EX`` reservation.

        Returns exactly one of ``Reserved`` / ``InFlight`` /
        ``CachedOutcome`` / ``BodyMismatchReservation``. The endpoint
        pattern-matches; only the ``Reserved`` branch may call
        ``commit()`` or ``release()``.
        """
        redis_key = self._build_key(user_id, key)
        marker = json.dumps(
            {
                "state": "pending",
                "body_hash": body_hash,
                "at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        # SET NX EX — atomic reserve. Returns truthy if set, None if the
        # key already exists.
        was_set = await self._redis.set(redis_key, marker, nx=True, ex=RESERVATION_TTL_S)
        if was_set:
            return Reserved(redis_key=redis_key)

        existing = await self._redis.get(redis_key)
        if existing is None:
            # Race: the key expired between SET NX and GET. Retry
            # once — this is bounded (at most one extra SETNX) because
            # on the second call the key is either fresh (we win) or
            # a concurrent caller has written a new pending marker.
            was_set_retry = await self._redis.set(redis_key, marker, nx=True, ex=RESERVATION_TTL_S)
            if was_set_retry:
                return Reserved(redis_key=redis_key)
            existing = await self._redis.get(redis_key)
            if existing is None:
                # Give up cleanly — two expiries in a row is pathological;
                # treat as in-flight and let the caller retry.
                return InFlight()

        decoded = json.loads(existing.decode("utf-8") if isinstance(existing, bytes) else existing)
        if decoded.get("state") == "pending":
            # Another request holds the reservation. Body mismatch on
            # an in-flight pending means the SAME key is being used
            # concurrently with a different body — still body-mismatch,
            # not in-flight, because the caller cannot recover by
            # retrying (the key is locked to the first body).
            if decoded.get("body_hash") != body_hash:
                return BodyMismatchReservation()
            return InFlight()

        # Completed cacheable outcome path.
        if decoded.get("body_hash") != body_hash:
            return BodyMismatchReservation()
        outcome_data = decoded["outcome"]
        outcome = EndpointOutcome(
            status_code=outcome_data["status_code"],
            response=outcome_data["response"],
            cacheable=outcome_data["cacheable"],
            failure_kind=FailureKind(outcome_data["failure_kind"]),
        )
        return CachedOutcome(outcome=outcome)

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    async def commit(
        self,
        redis_key: str,
        body_hash: str,
        outcome: EndpointOutcome,
    ) -> None:
        """Cache the outcome at ``redis_key`` for 24 h.

        Raises ``ValueError`` if called with a non-cacheable outcome
        — that's a programming error (the endpoint should have called
        ``release()`` instead).
        """
        if not outcome.cacheable:
            raise ValueError(
                f"commit() called with a non-cacheable outcome "
                f"(status={outcome.status_code}, "
                f"failure_kind={outcome.failure_kind.value}). "
                f"Use release() for transient outcomes."
            )
        payload = json.dumps(
            {
                "state": "completed",
                "body_hash": body_hash,
                "outcome": {
                    "status_code": outcome.status_code,
                    "response": outcome.response,
                    "cacheable": outcome.cacheable,
                    "failure_kind": outcome.failure_kind.value,
                },
                "at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        await self._redis.set(redis_key, payload, ex=RESPONSE_TTL_S)

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release(self, redis_key: str) -> None:
        """Delete the reservation. Called on transient outcomes
        (``IN_FLIGHT`` / ``HALT_ACTIVE`` / ``API_POLL_TIMEOUT``) and
        on raised exceptions. After release, the next retry with
        the same key will ``SET NX`` a fresh slot."""
        await self._redis.delete(redis_key)
