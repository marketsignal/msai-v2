"""Tests for the FastAPI lifespan's fail-closed GATEWAY_CONFIG check (PR 1 T5).

Council 2026-05-29 blocking objection #13: a misconfigured ``GATEWAY_CONFIG``
(e.g., duplicate ``ib_login_key``) MUST exit the backend non-zero within 10s
of boot, not silently degrade to picking one of the duplicates.
"""

from __future__ import annotations

import pytest

from msai.main import app, lifespan


@pytest.mark.asyncio
async def test_startup_fails_closed_on_duplicate_login(monkeypatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_CONFIG",
        "marin1016test:host-a:4004,marin1016test:host-b:4005",
    )
    with pytest.raises(ValueError, match="duplicate ib_login_key"):
        async with lifespan(app):
            pass  # lifespan-enter MUST raise; we should never reach here


@pytest.mark.asyncio
async def test_startup_accepts_valid_single_login_with_accounts(monkeypatch) -> None:
    # Lifespan-enter completes; we don't exercise endpoints here — only
    # boot. NB: lifespan also starts ib_probe + IB account snapshot tasks
    # which DO NOT require IB Gateway up (best-effort, retried on /ready).
    monkeypatch.setenv(
        "GATEWAY_CONFIG",
        "marin1016test:ib-gateway:4002:accounts=DUP733214|DUP733215",
    )
    async with lifespan(app):
        assert app.state.gateway_router is not None
        assert app.state.gateway_router.login_keys == ["marin1016test"]
        assert app.state.gateway_router.accounts_for("marin1016test") == [
            "DUP733214",
            "DUP733215",
        ]
