"""Capabilities established for the installed native runtime.

The RC6 application is a bounded research candidate. An environment flag
cannot grant live acceptance or make V1 cache/event formats compatible.
"""

from importlib.metadata import version
from typing import Any

from fastapi import Depends, HTTPException

from msai.core.auth import get_current_user


class UnsupportedRuntimeError(RuntimeError):
    """An operation requires native contracts not accepted for this runtime."""


def engine_version() -> str:
    return version("nautilus_trader")


def is_v2_research_runtime() -> bool:
    return engine_version().split(".", 1)[0] == "2"


def require_live_runtime(operation: str) -> None:
    if is_v2_research_runtime():
        raise UnsupportedRuntimeError(
            f"{operation} is unsupported on Nautilus {engine_version()}: research-only runtime; "
            "live adapters, cache and event projection have not been accepted."
        )


async def require_live_user(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Authenticate before refusal, ahead of DB/Redis route dependencies."""
    try:
        require_live_runtime("Live operation / installation")
    except UnsupportedRuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "unsupported_nautilus_runtime", "message": str(exc)},
        ) from exc
    return claims
