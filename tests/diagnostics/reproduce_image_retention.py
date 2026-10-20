#!/usr/bin/env python3
"""Run only the investigator's immutable source primary or control check."""

from __future__ import annotations

import argparse
import subprocess
import sys

BASE = "fb75795317a7568f276040b5482fae3ec55c9c30"
PATTERNS = {
    "primary": r"docker compose .*pull|docker (image|system|builder) (rm|prune)|docker rmi|image-retention",
    "control": r'^readonly IMAGES_(LAST_GOOD|ENV_PERSIST)=|cp -p "\$IMAGES_(ENV|LAST_GOOD)" "\$IMAGES_(LAST_GOOD|ENV)"|cp -p "\$IMAGES_ENV" "\$\{IMAGES_ENV_PERSIST\}',
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=PATTERNS)
    args = parser.parse_args()
    paths = ["scripts/deploy-on-vm.sh"]
    if args.check == "primary":
        paths.append(".github/workflows/deploy.yml")
    result = subprocess.run(
        ["git", "grep", "-nE", PATTERNS[args.check], BASE, "--", *paths],
        capture_output=True,
        check=False,
    )
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
