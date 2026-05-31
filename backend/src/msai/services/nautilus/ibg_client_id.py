"""Deterministic ``ibg_client_id`` derivation from a deployment slug.

Extracted from :mod:`msai.services.nautilus.live_node_config` (Codex iter 1
P1-5 of PR 1 — multi-account-broker-fleet). The derivation lives in its
own module so two unrelated call sites can re-derive the SAME integer
without reaching into the live-node config builder:

1. The TradingNode config builders (``build_live_trading_node_config``,
   ``build_portfolio_trading_node_config``, ``build_per_account_trading_node_config``)
   that need a stable ``ibg_client_id`` for the IB Gateway connection.

2. The ``/api/v1/live/status`` serializer + CLI + UI surface introduced
   in PR 1 Task T14. Neither ``LiveDeployment`` nor ``LiveNodeProcess``
   persists the value, but downstream observability needs to display
   which IB Gateway client slot a deployment is occupying. A
   deterministic re-derivation helper avoids a schema migration just to
   surface a value that's already implied by the deployment_slug.

IB ``client_id`` is a signed 32-bit int; we mask to 31 bits to avoid the
high bit (some IB middleware doesn't like negative ids). The ``role``
salt (``"data"`` or ``"exec"``) is mixed in via sha256 so two clients on
the same deployment can never collide regardless of slug structure
(NautilusTrader gotcha #3).

Zero is mapped to 1 because IB Gateway treats ``client_id=0`` as a
privileged "master" connection — we never want to claim that slot by
accident.

Determinism matters: the same ``(deployment_slug, role)`` pair MUST
always produce the same id so a restart reconnects under the SAME client
identity — otherwise IB Gateway sees a "new" connection and the old
client's open orders + subscriptions get stranded.
"""

from __future__ import annotations

import hashlib

# The two well-known role salts. Kept as module constants so callers in
# different files can't accidentally drift on the role string (e.g.
# ``"exec"`` vs ``"execution"`` would silently break IB Gateway client
# uniqueness).
ROLE_DATA: str = "data"
ROLE_EXEC: str = "exec"


def derive_ibg_client_id(deployment_slug: str, role: str = ROLE_EXEC) -> int:
    """Return a stable 31-bit positive integer for the (slug, role) pair.

    Args:
        deployment_slug: The 16-char hex slug persisted on
            ``LiveDeployment.deployment_slug``.
        role: Either :data:`ROLE_DATA` or :data:`ROLE_EXEC`. Defaults to
            ``"exec"`` because the per-account fleet topology (PR 1 T11)
            only spawns ONE IB exec client per account; the legacy paths
            that need both data + exec ids pass the role explicitly.

    Returns:
        A positive integer in the range ``[1, 2**31 - 1]``. ``0`` is
        mapped to ``1`` to avoid colliding with IB Gateway's privileged
        master connection slot.
    """
    digest = hashlib.sha256(deployment_slug.encode("ascii") + role.encode("ascii")).digest()
    raw = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return raw or 1
