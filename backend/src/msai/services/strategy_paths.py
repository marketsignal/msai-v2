"""Portable resolution for strategy paths stored in the registry."""

from __future__ import annotations

from pathlib import Path

from msai.core.config import settings


def resolve_strategy_file(
    file_path: str | Path,
    *,
    strategies_root: Path | None = None,
) -> Path:
    """Resolve a stored strategy path independently of the process cwd.

    Canonical registry values are relative to ``strategies_root``.  Absolute
    paths remain readable during rolling upgrades, and the historical
    ``strategies/...`` relative form is accepted without duplicating the root
    directory segment.
    """
    path = Path(file_path).expanduser()
    if path.is_absolute():
        return path.resolve()

    root = (strategies_root or settings.strategies_root).expanduser().resolve()
    if path.parts and path.parts[0] == root.name:
        path = Path(*path.parts[1:])
    return (root / path).resolve()
