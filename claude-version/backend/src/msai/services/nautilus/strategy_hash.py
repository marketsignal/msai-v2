"""Strategy code hash — SHA256 of the strategy file bytes.

Phase 1 task 1.12 (Codex finding #7). Used for reproducibility
on every backtest and live deployment: the hash is stored on
``live_deployments.strategy_code_hash`` and
``order_attempt_audits.strategy_code_hash`` so a restart or a
backtest can always resolve "what code ran when".

**Why not ``git rev-parse``**: the production container only mounts
``src/`` and ``strategies/``, not the repo root. ``git`` is not
available inside the container at all. A file-bytes SHA256 works
regardless of deployment topology, is reproducible from any build
artifact, and is trivially comparable without a git client.

This module is a thin wrapper over the existing
:func:`msai.services.strategy_registry.compute_file_hash` helper —
it exists to give live-trading callers a dedicated, well-documented
import path (``nautilus.strategy_hash.compute_strategy_code_hash``)
and to add structured error handling + path-not-found diagnostics
that the registry helper doesn't bother with.
"""

from __future__ import annotations

import hashlib
from pathlib import Path  # noqa: TC003 — runtime use for type + isinstance


class StrategyFileNotFoundError(FileNotFoundError):
    """Raised when the strategy file path doesn't exist on disk.

    Subclasses ``FileNotFoundError`` so callers that already catch
    the stdlib exception (e.g. the ``/api/v1/live/start`` handler)
    pick it up, but adds the strategy-specific context to the
    message so log triage points directly at the missing file.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"Strategy file not found on disk: {path}. "
            "Re-register the strategy or restore the source file."
        )
        self.path = path


def compute_strategy_code_hash(path: Path) -> str:
    """SHA256 of the strategy file's bytes, as a lowercase hex string.

    Args:
        path: Absolute path to the ``.py`` file on disk. Callers in
            the API process are responsible for resolving a
            DB-stored relative path (``strategies/example/ema_cross.py``)
            against ``settings.strategies_root`` BEFORE calling this —
            this helper deliberately does NOT do path resolution so
            it's pure, testable, and has no dependency on settings.

    Returns:
        A 64-character lowercase hex string.

    Raises:
        StrategyFileNotFoundError: If ``path`` doesn't exist or isn't
            a regular file.
    """
    if not path.is_file():
        raise StrategyFileNotFoundError(path)

    sha = hashlib.sha256()
    with path.open("rb") as handle:
        # Read in 8 KiB chunks so memory stays constant even for
        # strategies that bundle a lot of helper code.
        for chunk in iter(lambda: handle.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def hashes_match(a: str, b: str) -> bool:
    """Constant-time compare of two hex strings.

    Used by reconciliation paths that verify a running deployment's
    code version hasn't drifted. ``hmac.compare_digest`` gives
    constant-time behavior for equal-length strings; short-circuit
    on length mismatch for clarity.
    """
    import hmac

    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)
