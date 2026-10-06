"""The CI launcher rejects bad research selections before pytest can collect."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/run_backend_ci.py"
spec = importlib.util.spec_from_file_location("backend_ci", SCRIPT)
assert spec and spec.loader
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)


def test_exact_rc6_manifest_selects_real_unique_files() -> None:
    paths = suite.select_tests(
        "2.0.0rc6",
        SCRIPT.parent / "nautilus_v2_research_tests.txt",
        SCRIPT.parent.parent / "backend",
    )
    assert len(paths) == 24
    assert len(set(paths)) == len(paths)
    assert "tests/unit/test_nautilus_runtime_capabilities.py" in paths
    assert "tests/unit/test_v2_legacy_live_boundary.py" in paths


@pytest.mark.parametrize("version", ["2.0.0rc7", "2.0.0", "3.0.0", "unknown"])
def test_unknown_runtime_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        suite.select_tests(version, Path("unused"), Path("unused"))


@pytest.mark.parametrize(
    "content",
    ["", "tests/unit/a.py\ntests/unit/a.py\n", "tests/unit/missing.py\n", "../outside.py\n"],
)
def test_invalid_manifest_rejected(tmp_path: Path, content: str) -> None:
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/a.py").touch()
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(content)
    with pytest.raises(ValueError):
        suite.select_tests("2.0.0rc6", manifest, tmp_path)


def test_v1_retains_full_suite_route() -> None:
    assert suite.select_tests("1.223.0", Path("unused"), Path("unused")) == ["tests/"]


def test_rejection_never_launches_pytest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(suite, "version", lambda _: "2.0.0rc7")
    launch = Mock()
    monkeypatch.setattr(suite.os, "execv", launch)
    assert suite.main(["-v", "--cov=msai"]) == 2
    launch.assert_not_called()
    assert "Unsupported" in capsys.readouterr().err


def test_pytest_status_propagates_from_launcher(tmp_path: Path) -> None:
    # Real process replacement: a tiny pytest stand-in exits nonzero. No shell expansion.
    (tmp_path / "pytest.py").write_text("raise SystemExit(7)\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys; from unittest.mock import patch; "
            f"sys.path.insert(0, {str(tmp_path)!r}); "
            "p=patch('importlib.metadata.version', return_value='1.223.0'); p.start(); "
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
        ],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7, result.stderr


def test_selection_rejection_exits_nonzero_in_actual_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; from unittest.mock import patch; "
            "p=patch('importlib.metadata.version', return_value='2.0.0rc7'); p.start(); "
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "selection refused" in result.stderr
