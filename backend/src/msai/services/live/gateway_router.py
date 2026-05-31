"""GatewayRouter -- resolve ib_login_key to (host, port) + accounts for IB Gateway containers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayEndpoint:
    host: str
    port: int


class GatewayRouter:
    """Resolves ``ib_login_key -> (host, port, accounts)`` from a static config string.

    Config format (env var ``GATEWAY_CONFIG``)::

        login1:host1:port1[:accounts=A1|A2|...],login2:host2:port2[:accounts=B1|...]

    Examples::

        marin1016test:ib-gateway:4002:accounts=DUP733214|DUP733215
        marin1016test:ib-gateway:4002:accounts=DUP733214|DUP733215,mslvp000:ib-gateway-lvp:4003:accounts=U1234567

    Backwards-compatible with the legacy 3-tuple form ``login:host:port``
    (no accounts segment) which yields ``accounts_for(login) == []``.

    Fail-closed (council 2026-05-29 blocking objection #13): two entries
    with the same ``ib_login_key`` raise ``ValueError`` at parse time.
    Malformed entries (fewer than 3 colon-separated fields) ALSO raise
    ``ValueError`` so a typo in ``GATEWAY_CONFIG`` (e.g.
    ``marin1016test_typo:ib-gateway:4002:accounts=...`` with a missing
    colon) fails fast at boot instead of silently dropping the entry
    and falling back to ``settings.ib_host/port`` (F6 fix — Codex iter 2
    P1 / silent-failure-hunter F8).
    """

    def __init__(self, config_str: str | None = None) -> None:
        self._routes: dict[str, GatewayEndpoint] = {}
        self._accounts: dict[str, list[str]] = {}
        if not config_str:
            return
        for entry in config_str.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 3:
                # F6 fix: fail-closed (council 2026-05-29 obj #13). Silent
                # skip is a misconfig amplifier — a single missed colon in
                # an env var lands the deployment on the process-wide
                # settings (potentially wrong port + wrong account), with
                # no log signal at startup. Raise instead so the boot
                # lifespan fails fast and the operator sees the typo.
                raise ValueError(
                    f"malformed GATEWAY_CONFIG entry: {entry!r} "
                    "(expected login:host:port[:accounts=A1|A2|...])"
                )
            login_key = parts[0].strip()
            host = parts[1].strip()
            port = int(parts[2].strip())
            accounts: list[str] = []
            for extra in parts[3:]:
                stripped = extra.strip()
                if not stripped:
                    # Codex iter 17 P2: empty 4th+ token (e.g. a trailing
                    # colon like ``marin1016test:host:port:``) is a typo,
                    # not "no binding". Operator-intended unbound logins
                    # use the 3-tuple form WITHOUT a trailing colon —
                    # raise so the typo is caught at boot.
                    raise ValueError(
                        f"malformed GATEWAY_CONFIG segment in entry {entry!r}: "
                        "empty token after ``login:host:port`` (likely a "
                        "trailing colon). Use ``login:host:port`` for unbound "
                        "logins, or ``login:host:port:accounts=A|B`` for bound."
                    )
                # Codex iter 16 P2: fail-closed on malformed binding
                # segments. Typos like ``account=DUP1`` (missing ``s``) or
                # garbage tokens used to silently fall through, leaving
                # ``accounts_for()`` returning ``[]`` so the supervisor
                # opted OUT of the binding-enforcement check. Now a
                # 4th+ segment that isn't ``accounts=...`` raises.
                if not stripped.startswith("accounts="):
                    raise ValueError(
                        f"malformed GATEWAY_CONFIG segment in entry {entry!r}: "
                        f"extra token {extra!r} — only ``accounts=A1|A2|...`` "
                        "is permitted after ``login:host:port``."
                    )
                # Pipe-separated to avoid colliding with the comma used
                # between gateway entries (PR 1 T4 / Codex iter 1 P1-2).
                accounts = [a.strip() for a in stripped[len("accounts=") :].split("|") if a.strip()]
                if not accounts:
                    raise ValueError(
                        f"malformed GATEWAY_CONFIG segment in entry {entry!r}: "
                        "``accounts=`` segment must list at least one account_id "
                        "(empty bindings are not permitted — use ``login:host:port`` "
                        "without the segment for unbound logins)."
                    )
            if login_key in self._routes:
                raise ValueError(f"duplicate ib_login_key {login_key!r} in GATEWAY_CONFIG")
            endpoint = GatewayEndpoint(host=host, port=port)
            # Codex iter 4 P2-3: also fail-closed on duplicate (host, port).
            # Two different ``ib_login_key``s pointing at the same physical
            # gateway would let concurrent spawns bypass the per-session
            # startup serialization (which is keyed by login). Refuse the
            # configuration at parse time.
            colliding_login = next(
                (
                    existing_login
                    for existing_login, existing_endpoint in self._routes.items()
                    if existing_endpoint == endpoint
                ),
                None,
            )
            if colliding_login is not None:
                raise ValueError(
                    f"duplicate gateway endpoint {host}:{port} in GATEWAY_CONFIG "
                    f"(already used by ib_login_key {colliding_login!r}); two logins "
                    f"sharing one physical gateway would bypass per-session "
                    f"startup serialization"
                )
            self._routes[login_key] = endpoint
            self._accounts[login_key] = accounts

    def resolve(self, ib_login_key: str) -> GatewayEndpoint:
        """Return the gateway endpoint for *ib_login_key*, or raise ``ValueError``."""
        if ib_login_key not in self._routes:
            raise ValueError(
                f"No gateway configured for IB login '{ib_login_key}'. "
                f"Available: {list(self._routes.keys())}"
            )
        return self._routes[ib_login_key]

    def accounts_for(self, ib_login_key: str) -> list[str]:
        """Return the configured account_ids for *ib_login_key*, or [] if none."""
        return list(self._accounts.get(ib_login_key, []))

    @property
    def is_multi_login(self) -> bool:
        """True when more than one IB login is configured."""
        return len(self._routes) > 1

    @property
    def login_keys(self) -> list[str]:
        """Return all configured IB login keys."""
        return list(self._routes.keys())
