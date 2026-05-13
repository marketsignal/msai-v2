"""Structural assertions on ``docker-compose.prod.yml`` for the IB_PORT bug fix.

The 2026-05-12 paper-drill preflight discovered that prod compose set
``IB_PORT=${IB_PORT:-4002}`` for client services. The gnzsnz/ib-gateway
image binds IB Gateway to ``127.0.0.1:4002`` (paper) — refusing direct
network connections — and exposes a ``socat`` proxy on ``0.0.0.0:4004``
that re-originates as localhost. Cross-container clients MUST target the
socat port (4004 paper / 4003 live), not the loopback bind port. The fix
also decouples the gateway's ``IB_API_PORT`` from the client-side
``IB_PORT`` so flipping to live mode requires two explicit overrides
instead of one ambiguous one.

These tests parse ``docker-compose.prod.yml`` as YAML (no docker daemon
required) and assert structurally on the env defaults + port mapping.
They run in CI as part of the backend pytest suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def compose_prod() -> dict:
    """Parse ``docker-compose.prod.yml`` from the repo root."""
    repo_root = Path(__file__).resolve().parents[3]
    compose_path = repo_root / "docker-compose.prod.yml"
    assert compose_path.exists(), f"missing {compose_path}"
    with compose_path.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def compose_prod_text() -> str:
    """Raw text of ``docker-compose.prod.yml`` for comment-level checks."""
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / "docker-compose.prod.yml").read_text()


def _env_default(env_value: str) -> str:
    """Extract the default from a ``${VAR:-default}`` interpolation, or
    return the value unchanged if there's no default.
    """
    match = re.match(r"^\$\{[A-Z_][A-Z0-9_]*:-([^}]*)\}$", env_value)
    return match.group(1) if match else env_value


class TestIBPortDefaults:
    """The 2026-05-12 paper-drill regression guard."""

    def test_backend_ib_port_defaults_to_socat_paper(self, compose_prod: dict) -> None:
        """Backend connects to ``ib-gateway:${IB_PORT}``. Default must be 4004
        (socat paper proxy) — 4002 (IB Gateway loopback bind) silently TCP-
        connects but the API handshake never completes."""
        backend_env = compose_prod["services"]["backend"]["environment"]
        assert _env_default(backend_env["IB_PORT"]) == "4004", (
            f"backend.IB_PORT default must be 4004; got {backend_env['IB_PORT']}"
        )

    def test_live_supervisor_ib_port_defaults_to_socat_paper(self, compose_prod: dict) -> None:
        """live-supervisor connects to ``ib-gateway:${IB_PORT}``. Same as backend."""
        sup_env = compose_prod["services"]["live-supervisor"]["environment"]
        assert _env_default(sup_env["IB_PORT"]) == "4004", (
            f"live-supervisor.IB_PORT default must be 4004; got {sup_env['IB_PORT']}"
        )

    def test_ib_gateway_api_port_decoupled_from_client_ib_port(self, compose_prod: dict) -> None:
        """ib-gateway's ``IB_API_PORT`` (passed to gnzsnz; controls what
        port IB Gateway binds INSIDE the container) MUST be decoupled from
        client-side ``IB_PORT`` (the socat proxy port). Conflating them was
        the original bug — paper requires gateway=4002 + clients=4004.
        """
        gw_env = compose_prod["services"]["ib-gateway"]["environment"]
        ib_api_port_raw = gw_env["IB_API_PORT"]
        assert _env_default(ib_api_port_raw) == "4002", (
            f"ib-gateway.IB_API_PORT default must be 4002 (paper loopback bind); "
            f"got {ib_api_port_raw}"
        )
        # The interpolation must reference IB_API_PORT, not IB_PORT —
        # otherwise it's still coupled.
        assert "IB_API_PORT" in ib_api_port_raw and "IB_PORT}" not in ib_api_port_raw.replace(
            "IB_API_PORT}", ""
        ), (
            f"ib-gateway.IB_API_PORT must interpolate ${{IB_API_PORT:-...}}, "
            f"not ${{IB_PORT:-...}}; got {ib_api_port_raw}"
        )

    def test_ib_gateway_host_port_exposes_socat_proxy(self, compose_prod: dict) -> None:
        """The host-side port mapping must expose the socat proxy port
        (4004 paper) so ``curl 127.0.0.1:4004`` on the VM actually reaches
        a working endpoint. Mapping the IB Gateway's loopback-only bind
        port (4002) was a no-op."""
        gw_ports = compose_prod["services"]["ib-gateway"]["ports"]
        assert gw_ports, "ib-gateway must declare at least one port mapping"
        # The IB_PORT mapping is the first entry; 5900 (VNC) is the second.
        first = gw_ports[0]
        if isinstance(first, str):
            # Short form: "127.0.0.1:${IB_PORT:-4004}:${IB_PORT:-4004}".
            # Extract the default after the second-to-last colon's `${...}`.
            assert ":-4004}" in first, (
                f"ib-gateway port mapping must default to socat paper port (4004); got {first!r}"
            )
        else:
            # Long form: dict with target/published/host_ip.
            for key in ("target", "published"):
                value = first.get(key, "")
                assert _env_default(str(value)) == "4004", (
                    f"ib-gateway port mapping {key} must default to 4004; got {value}"
                )

    def test_misleading_pre_2026_05_09_comment_removed(self, compose_prod_text: str) -> None:
        """The PR #50 comment claiming the 4004 default "never matched any
        ib-gateway listener" was a misdiagnosis. 4004 IS what works (socat
        proxy). Comment must be gone so future readers aren't misled."""
        # The comment wraps across two lines in the actual file, so check
        # the load-bearing phrase only.
        assert "Pre-2026-05-09" not in compose_prod_text, (
            "remove the misleading PR #50 comment about a phantom 4004 default"
        )
