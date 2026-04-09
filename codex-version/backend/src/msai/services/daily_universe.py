from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from msai.core.config import settings
from msai.services.daily_ingest import DAILY_INGEST_REQUESTS, DailyIngestRequest


class DailyUniverseService:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.daily_universe_path

    def list_requests(self) -> list[DailyIngestRequest]:
        if not self.path.exists():
            return list(DAILY_INGEST_REQUESTS)
        payload = json.loads(self.path.read_text())
        requests = payload.get("requests") or []
        return [
            DailyIngestRequest(
                asset_class=str(item["asset_class"]),
                symbols=[str(symbol) for symbol in item.get("symbols", []) if str(symbol).strip()],
                provider=str(item["provider"]),
                dataset=str(item["dataset"]),
                schema=str(item.get("schema") or "ohlcv-1m"),
            )
            for item in requests
        ]

    def save_requests(self, requests: list[DailyIngestRequest]) -> list[DailyIngestRequest]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "requests": [asdict(request) for request in requests],
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return requests
