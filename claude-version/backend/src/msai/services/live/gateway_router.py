"""GatewayRouter -- resolve ib_login_key to (host, port) for IB Gateway containers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayEndpoint:
    host: str
    port: int


class GatewayRouter:
    """Resolves ``ib_login_key -> (host, port)`` from a static config string.

    Config format (env var ``GATEWAY_CONFIG``)::

        login1:host1:port1,login2:host2:port2

    Example::

        marin1016test:ib-gateway-paper:4004,mslvp000:ib-gateway-lvp:4003
    """

    def __init__(self, config_str: str | None = None) -> None:
        self._routes: dict[str, GatewayEndpoint] = {}
        if config_str:
            for entry in config_str.split(","):
                parts = entry.strip().split(":")
                if len(parts) == 3:
                    self._routes[parts[0].strip()] = GatewayEndpoint(
                        host=parts[1].strip(),
                        port=int(parts[2].strip()),
                    )

    def resolve(self, ib_login_key: str) -> GatewayEndpoint:
        """Return the gateway endpoint for *ib_login_key*, or raise ``ValueError``."""
        if ib_login_key not in self._routes:
            raise ValueError(
                f"No gateway configured for IB login '{ib_login_key}'. "
                f"Available: {list(self._routes.keys())}"
            )
        return self._routes[ib_login_key]

    @property
    def is_multi_login(self) -> bool:
        """True when more than one IB login is configured."""
        return len(self._routes) > 1

    @property
    def login_keys(self) -> list[str]:
        """Return all configured IB login keys."""
        return list(self._routes.keys())
