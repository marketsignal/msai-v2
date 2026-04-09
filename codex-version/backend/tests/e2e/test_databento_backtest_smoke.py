from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

RUN_RESEARCH_E2E = os.getenv("RUN_RESEARCH_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_RESEARCH_E2E,
    reason="set RUN_RESEARCH_E2E=1 to run Databento-backed backtest E2E smoke tests",
)


def test_databento_equity_backtest_smoke(tmp_path: Path) -> None:
    runner = Path(__file__).with_name("databento_backtest_smoke_runner.py")
    env = os.environ.copy()
    env["RESEARCH_E2E_DATA_ROOT"] = str(tmp_path / "data")

    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    if result.returncode != 0:
        raise AssertionError(
            "Databento backtest smoke failed.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
