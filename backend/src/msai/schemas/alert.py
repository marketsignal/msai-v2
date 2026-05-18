"""Pydantic schemas for the alerts API.

iter-5 verify-e2e Issue D (P2): the previous schema omitted ``id`` and
total count, which deviated from ``.claude/rules/api-design.md``'s
pagination envelope. ``id`` is now derived from a stable SHA256 hash of
``type+title+created_at`` — no storage migration required (file-backed
alerts have no native id), and the hash is stable across reads. ``total``
counts the records returned (alerts don't paginate; ``limit`` clamps the
slice). UI consumers (R19/R22 snapshot-into-local-state) still iterate
by array index so this change is additive and non-breaking.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, computed_field


class AlertRecord(BaseModel):
    type: str
    level: str
    title: str
    message: str
    created_at: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Stable opaque id derived from type+title+created_at.

        Same alert across reads produces the same id — useful for
        client-side dedup / permalink construction. NOT cryptographically
        meaningful; just a stable index handle.
        """
        digest = hashlib.sha256(f"{self.type}|{self.title}|{self.created_at}".encode())
        return digest.hexdigest()[:16]


class AlertListResponse(BaseModel):
    alerts: list[AlertRecord] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        """Number of alerts returned in this response.

        Alerts don't truly paginate (``limit``-only clamp on the slice),
        so ``total`` here is "records in this response" rather than
        "records that exist." For the dashboard's last-N consumption
        that's the operative count.
        """
        return len(self.alerts)
