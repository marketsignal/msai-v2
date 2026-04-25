"""Smoke test: T11 wiring — symbol-onboarding routes are mounted on the real app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from msai.main import app


def test_onboard_routes_mounted() -> None:
    """All four /api/v1/symbols/* routes resolve on the real app.

    We don't care about response status here — only that FastAPI's
    routing layer has them registered (the alternative is a 404
    NOT_FOUND_ROUTE indicating router was never included).
    """
    client = TestClient(app, raise_server_exceptions=False)
    paths = [
        ("POST", "/api/v1/symbols/onboard/dry-run"),
        ("POST", "/api/v1/symbols/onboard"),
        ("GET", "/api/v1/symbols/onboard/00000000-0000-0000-0000-000000000000/status"),
        ("GET", "/api/v1/symbols/readiness"),
    ]
    for method, path in paths:
        resp = client.request(method, path)
        # 404 with a non-empty body proves the route exists (validation /
        # auth fired). A "true" 404 — route not registered — returns the
        # default Starlette payload `{"detail":"Not Found"}`.
        if resp.status_code == 404:
            assert resp.json() != {"detail": "Not Found"}, f"Route not registered: {method} {path}"


def test_asset_universe_routes_are_gone() -> None:
    """Router was deleted in T11 — `/api/v1/universe` must return Starlette default 404."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/universe")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
