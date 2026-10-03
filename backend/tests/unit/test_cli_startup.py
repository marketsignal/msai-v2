"""Exercise CLI imports in fresh processes without reading developer secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
_SENTINEL = "synthetic-private-input-marker"


def _run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Deliberately do not inherit credentials or configuration from this process.
    return subprocess.run(
        [sys.executable, "-m", "msai.cli", *args],
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(_BACKEND_SRC),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
            "COLUMNS": "120",
        },
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


@pytest.mark.parametrize("working_directory", [".", "backend"])
def test_help_starts_from_shared_compose_project(
    tmp_path: Path, working_directory: str
) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env").write_text(
        f"TWS_PASSWORD={_SENTINEL}\n"
        "TRADING_MODE=paper\n"
        "MSAI_API_URL=http://localhost:8800\n"
        "NEXT_PUBLIC_API_URL=http://localhost:8800\n",
        encoding="utf-8",
    )

    result = _run_cli(tmp_path / working_directory, "backtest", "--help")

    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout
    assert "results" in result.stdout
    assert _SENTINEL not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("dotenv", "field"),
    [
        (f"IB_PORT={_SENTINEL}\n", "IB_PORT"),
        (f"BROKER_GATEWAY_SLOTS=[{_SENTINEL}]\n", "BROKER_GATEWAY_SLOTS"),
        (f"CORS_ORIGINS=[{_SENTINEL}]\n", "cors_origins"),
        (
            f"ENVIRONMENT=production\nREPORT_SIGNING_SECRET={_SENTINEL}\n",
            "REPORT_SIGNING_SECRET",
        ),
    ],
)
def test_invalid_settings_fail_without_echoing_input(
    tmp_path: Path, dotenv: str, field: str
) -> None:
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")

    result = _run_cli(tmp_path, "--help")

    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert field in diagnostic
    assert _SENTINEL not in diagnostic


def test_production_startup_accepts_valid_secret_with_compose_extras(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ENVIRONMENT=production\n"
        f"REPORT_SIGNING_SECRET={_SENTINEL}-sufficient-length\n"
        f"TWS_PASSWORD={_SENTINEL}\n",
        encoding="utf-8",
    )

    result = _run_cli(tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "backtest" in result.stdout
    assert _SENTINEL not in result.stdout + result.stderr
