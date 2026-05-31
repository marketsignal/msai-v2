"""Unit tests for :mod:`msai.services.nautilus.ibg_client_id`.

The derivation is load-bearing — both the live-node config builder and
(in Task T14) the ``/api/v1/live/status`` serializer re-derive the same
integer from the same deployment_slug. A bug here would either cause IB
Gateway client_id collisions (gotcha #3 — silent disconnect of the
older session) OR a mismatch between the surfaced status value and the
actual connected client_id (observability rot).
"""

from __future__ import annotations

from msai.services.nautilus.ibg_client_id import (
    ROLE_DATA,
    ROLE_EXEC,
    derive_ibg_client_id,
)


def test_same_slug_same_role_yields_same_id() -> None:
    """Determinism: restarting the SAME deployment under the SAME role
    MUST yield the SAME ibg_client_id. Otherwise IB Gateway sees a
    "new" client and the old session's open orders / subscriptions get
    stranded."""
    slug = "a1b2c3d4e5f60718"
    assert derive_ibg_client_id(slug, ROLE_EXEC) == derive_ibg_client_id(slug, ROLE_EXEC)


def test_different_slugs_yield_different_ids() -> None:
    """Two concurrent deployments under the same role MUST NOT collide
    (gotcha #3). The sha256 source guarantees this for any reasonable
    pair of inputs — we just spot-check with two real-shaped slugs."""
    id_a = derive_ibg_client_id("a1b2c3d4e5f60718", ROLE_EXEC)
    id_b = derive_ibg_client_id("bbbbccccddddeeee", ROLE_EXEC)
    assert id_a != id_b


def test_same_slug_data_and_exec_differ() -> None:
    """Data and exec on the SAME deployment MUST NOT collide on the
    ibg_client_id slot. The role salt is what guarantees this."""
    slug = "a1b2c3d4e5f60718"
    assert derive_ibg_client_id(slug, ROLE_DATA) != derive_ibg_client_id(slug, ROLE_EXEC)


def test_default_role_is_exec() -> None:
    """The PR 1 fleet topology only spawns ONE IB client per account —
    the exec client. The default role therefore defaults to ``"exec"``
    so single-client call sites can omit the keyword argument."""
    slug = "a1b2c3d4e5f60718"
    assert derive_ibg_client_id(slug) == derive_ibg_client_id(slug, ROLE_EXEC)


def test_result_is_positive_31_bit_int() -> None:
    """IB ``client_id`` is signed 32-bit; the derivation masks to 31
    bits to avoid the high bit (some IB middleware doesn't like
    negative ids). The result MUST be a positive int in
    ``[1, 2**31 - 1]``. Zero must be remapped to 1 (we never claim
    IB's privileged master-connection slot by accident)."""
    # Spot-check a couple of slugs. The zero-remap branch is hard to
    # exercise without a contrived sha256 collision, so we just confirm
    # the function's range invariant holds.
    for slug in ("a" * 16, "0" * 16, "deadbeefdeadbeef"):
        value = derive_ibg_client_id(slug, ROLE_EXEC)
        assert isinstance(value, int)
        assert 1 <= value <= (2**31 - 1)


def test_role_constants_are_distinct_strings() -> None:
    """Belt-and-braces: ``ROLE_DATA`` and ``ROLE_EXEC`` must be distinct
    string values. The sha256 derivation salts on these constants, so
    a typo here would silently undo the data-vs-exec uniqueness
    guarantee."""
    assert isinstance(ROLE_DATA, str)
    assert isinstance(ROLE_EXEC, str)
    assert ROLE_DATA != ROLE_EXEC
