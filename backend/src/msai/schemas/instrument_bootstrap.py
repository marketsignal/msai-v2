"""Pydantic request/response models for POST /api/v1/instruments/bootstrap.

Contract pins:
- provider is Literal["databento"]; polygon and ib are rejected at Pydantic.
- asset_class_override matches the DB's ck_instrument_definitions_asset_class
  CHECK constraint: equity | futures | fx | option (crypto not supported by
  this bootstrap path). 'etf' stores as 'equity'; 'future' (singular) is
  rejected — registry taxonomy is plural 'futures'.
- max_concurrent hard-capped at 3 until real Databento rate limits are
  measured against the plan entitlement.
- exact_ids is a mapping of SYMBOL -> canonical alias_string (e.g.
  "BRK.B" -> "BRK.B.XNYS") from a prior 422 ambiguity candidates[] list.
  NOT a numeric Databento instrument_id — the DBN u32 id is not surfaced
  by Nautilus's pyo3 Instrument in a stable attribute.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


class CandidateInfo(BaseModel):
    """One ambiguity candidate returned to the operator on a 422.

    Each field lets the operator retry with --exact-id SYMBOL:ALIAS_STRING
    or exact_ids={SYMBOL: alias_string}.
    """

    alias_string: str
    raw_symbol: str
    asset_class: str
    dataset: str


class BootstrapRequest(BaseModel):
    provider: Literal["databento"] = "databento"
    symbols: list[str] = Field(min_length=1, max_length=50)
    asset_class_override: Literal["equity", "futures", "fx", "option"] | None = None
    max_concurrent: int = Field(default=3, ge=1, le=3)
    exact_ids: dict[str, str] | None = None

    @field_validator("symbols")
    @classmethod
    def _well_formed(cls, v: list[str]) -> list[str]:
        for sym in v:
            if not (1 <= len(sym) <= 32) or not _SYMBOL_PATTERN.match(sym):
                raise ValueError(f"invalid symbol: {sym!r}")
        return v

    @model_validator(mode="after")
    def _exact_ids_subset(self) -> BootstrapRequest:
        if self.exact_ids:
            extra = set(self.exact_ids) - set(self.symbols)
            if extra:
                raise ValueError(f"exact_ids keys not in symbols: {sorted(extra)}")
        return self


_SUCCESSFUL_OUTCOMES_WIRE = frozenset({"created", "noop", "alias_rotated"})


class BootstrapResultItem(BaseModel):
    symbol: str
    outcome: Literal[
        "created",
        "noop",
        "alias_rotated",
        "ambiguous",
        "upstream_error",
        "unauthorized",
        "unmapped_venue",
        "rate_limited",
    ]
    registered: bool
    backtest_data_available: bool | None = None
    live_qualified: bool
    canonical_id: str | None = None
    dataset: str | None = None
    asset_class: str | None = None
    candidates: list[CandidateInfo] = Field(default_factory=list)
    diagnostics: str | None = None

    @model_validator(mode="after")
    def _readiness_invariants(self) -> BootstrapResultItem:
        is_success = self.outcome in _SUCCESSFUL_OUTCOMES_WIRE
        if is_success != self.registered:
            raise ValueError(f"outcome={self.outcome!r} but registered={self.registered!r}")
        if not is_success:
            if self.live_qualified:
                raise ValueError(f"failed outcome {self.outcome!r} cannot have live_qualified=True")
            if self.backtest_data_available:
                raise ValueError(
                    f"failed outcome {self.outcome!r} cannot have backtest_data_available=True"
                )
            if self.canonical_id is not None:
                raise ValueError(f"failed outcome {self.outcome!r} cannot have canonical_id set")
        if self.outcome == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous outcome requires at least 2 candidates")
        return self


class BootstrapSummary(BaseModel):
    total: int
    created: int
    noop: int
    alias_rotated: int
    failed: int


class BootstrapResponse(BaseModel):
    """Bootstrap response envelope.

    Summary fields are not validated against results — callers must use
    ``build_bootstrap_response`` to keep them in sync.
    """

    results: list[BootstrapResultItem]
    summary: BootstrapSummary


_FAILED_OUTCOMES = frozenset(
    {
        "ambiguous",
        "upstream_error",
        "unauthorized",
        "unmapped_venue",
        "rate_limited",
    }
)
# Invariant parity: the successful + failed partitions cover every outcome literal.
assert _SUCCESSFUL_OUTCOMES_WIRE.isdisjoint(_FAILED_OUTCOMES)


def build_bootstrap_response(items: list[BootstrapResultItem]) -> BootstrapResponse:
    """Helper — computes BootstrapSummary from result items and wraps in response."""
    summary = BootstrapSummary(
        total=len(items),
        created=sum(1 for r in items if r.outcome == "created"),
        noop=sum(1 for r in items if r.outcome == "noop"),
        alias_rotated=sum(1 for r in items if r.outcome == "alias_rotated"),
        failed=sum(1 for r in items if r.outcome in _FAILED_OUTCOMES),
    )
    return BootstrapResponse(results=items, summary=summary)
