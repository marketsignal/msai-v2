"""Run the declared backend suite for the actual installed Nautilus release.

Called from backend; rejects invalid selection before replacing this process
with pytest, so neither selector nor pytest errors can be swallowed by a shell.
"""

import os
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "scripts/nautilus_v2_research_tests.txt"


def select_tests(engine: str, manifest: Path, backend: Path) -> list[str]:
    if engine.startswith("1."):
        return ["tests/"]
    if engine != "2.0.0rc6":
        raise ValueError(f"Unsupported Nautilus CI runtime: {engine}")
    paths = [
        line.strip()
        for line in manifest.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Research manifest must be nonempty and unique")
    for name in paths:
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not name.startswith("tests/")
            or path.suffix != ".py"
            or not (backend / path).is_file()
        ):
            raise ValueError(f"Invalid research test path: {name}")
    return paths


def main(args: list[str] | None = None) -> int:
    try:
        engine = version("nautilus_trader")
        paths = select_tests(engine, MANIFEST, ROOT / "backend")
    except (ValueError, OSError) as exc:
        print(f"Backend CI selection refused: {exc}", file=sys.stderr)
        return 2
    if engine == "2.0.0rc6":
        print(
            f"RC6 research-only CI: {len(paths)} owning test files. "
            "Legacy live and full-suite acceptance remain unverified.",
            flush=True,
        )
    else:
        print(
            f"Nautilus {engine}: legacy full-suite route (V2-source compatibility unverified).",
            flush=True,
        )
    os.chdir(ROOT / "backend")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "pytest", *paths, *(sys.argv[1:] if args is None else args)],
    )
    return 0  # execv only returns by raising an error.


if __name__ == "__main__":
    raise SystemExit(main())
