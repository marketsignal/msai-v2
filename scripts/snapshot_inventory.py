#!/usr/bin/env python3
"""Snapshot the current /api/v1/symbols/inventory output for diffing
post-Scope B. Capture-before-change per Contrarian prereq #4.

Usage:
    python scripts/snapshot_inventory.py \
        --base-url http://localhost:8800 \
        --api-key "$MSAI_API_KEY" \
        --window 2024-01-01:2025-12-31 \
        --output tests/fixtures/coverage-pre-scope-b.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8800")
    p.add_argument("--api-key", required=True, help="MSAI_API_KEY")
    p.add_argument(
        "--window",
        required=True,
        help="ISO date pair separated by ':' (e.g. 2024-01-01:2025-12-31)",
    )
    p.add_argument("--output", required=True, type=Path)
    p.add_argument(
        "--asset-class",
        default=None,
        help="Optional asset_class filter (equity, futures, fx, ...)",
    )
    args = p.parse_args()

    start, end = args.window.split(":", 1)
    params: dict[str, str] = {"start": start, "end": end}
    if args.asset_class:
        params["asset_class"] = args.asset_class

    headers = {"X-API-Key": args.api_key}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"{args.base_url}/api/v1/symbols/inventory",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        rows = resp.json()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"window": args.window, "rows": rows}, indent=2, sort_keys=True))
    print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
