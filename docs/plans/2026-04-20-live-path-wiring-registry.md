# Live-Path Wiring onto Instrument Registry — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Wire MSAI's live-start path (`/api/v1/live/start-portfolio` → `live_supervisor` → IB preload builder) onto the DB-backed instrument registry via a new pure-read `lookup_for_live(symbols, as_of_date)` resolver, enabling live trading of any IB-qualifiable equity / ETF / FX / futures symbol with no code edits.

**Architecture:** Introduce `ResolvedInstrument` (typed, options-extensible) + `lookup_for_live()` in `backend/src/msai/services/nautilus/security_master/live_resolver.py`. Re-wire three call sites (supervisor line 281-285, `build_ib_instrument_provider_config()`, `live_node_config.py:478`) onto it. No Alembic migration. `canonical_instrument_id()` leaves the runtime path (stays in CLI/bootstrap seeding). Structured telemetry + Prometheus counter + WARN alerts on miss / ERROR alerts on incomplete. Real-money drill on U4705114 before merge.

**Tech Stack:** Python 3.12 + SQLAlchemy 2.0 (AsyncSession, `InstrumentRegistry.find_by_alias(as_of_date)` and `find_by_raw_symbol(asset_class=None)`) + pytest + NautilusTrader `InteractiveBrokersInstrumentProviderConfig.load_contracts: FrozenSet[IBContract]` + project's hand-rolled metrics registry (`msai.services.observability.get_registry().counter()` — NOT `prometheus_client`; `/metrics` endpoint renders via `MetricsRegistry.render()`) + project's file-backed `alerting_service.send_alert(level, title, message)` (sync; module-level singleton in `msai.services.alerting`).

**References:**

- PRD: `docs/prds/live-path-wiring-registry.md`
- Council verdict: `docs/decisions/live-path-registry-wiring.md`
- Research brief: `docs/research/2026-04-20-live-path-wiring-registry.md`
- Nautilus gotchas: `.claude/rules/nautilus.md` (#3 IB client_id, #4 venue naming, #13 stop≠close)

**Design invariants (do not violate):**

1. Resolver is **pure-read** — no IB qualifier calls, no upserts. Cold-miss is operator action (`msai instruments refresh`), not runtime.
2. `as_of_date: date` is **required, no default** — naive UTC regresses roll-day behavior (registry's `find_by_alias` defaults to UTC per `registry.py:60`).
3. `contract_spec` is constructed deterministically from registry fields + alias parsing — **no schema change, no Alembic migration**.
4. `canonical_instrument_id()` is removed from supervisor / IB preload builder / live_node_config runtime paths. It stays in `cli.py` + `instruments.py` + `security_master/service.py` cold-paths for CLI seeding.
5. Registry miss = fail fast with operator hint. No silent fallback. No retry.
6. Every `lookup_for_live` call emits one structured log line per symbol (success OR miss) + increments the counter.
7. Options/crypto asset classes raise `UnsupportedAssetClassError` at resolver boundary (PRD §4 US-004 edge case).

**Reality-matching notes (iter-2 fixes from plan-review):**

- **Alerting:** project uses `alerting_service.send_alert(level, title, message)` from `msai.services.alerting` — **sync, module-level singleton, positional kwargs only** (no `context`, no `await`). Call with `level="warning"|"error"`.
- **Metrics:** project has a **hand-rolled** metrics registry (`msai.services.observability.metrics.MetricsRegistry`) with `Counter.labels(**kwargs).inc()` — **no `prometheus_client` import**. Register via `_r = get_registry(); MY_COUNTER = _r.counter(name, help)`. Labels apply at increment time: `MY_COUNTER.labels(source="registry").inc()`.
- **Supervisor failure path:** the payload factory **raises** to `ProcessManager.spawn()` which catches `ValueError`/`ImportError`/`ModuleNotFoundError`/`FileNotFoundError`/`AttributeError` and calls `_mark_failed(row_id, reason, failure_kind=SPAWN_FAILED_PERMANENT)`. The `/api/v1/live/start-portfolio` endpoint polls `live_node_processes.failure_kind` + `error_message` and maps them to `EndpointOutcome`. There is **no `command_bus.publish_failure`**. Our resolver errors must subclass `ValueError` so they hit the permanent-catch branch.
- **Distinct HTTP error codes require new `FailureKind` enum values** (`REGISTRY_MISS`, `REGISTRY_INCOMPLETE`, `UNSUPPORTED_ASSET_CLASS`) — column is `String(32)` with `parse_or_unknown`, so additive. ProcessManager dispatches on exception type before calling `_mark_failed` with the specific kind.
- **API preflight is explicitly out of scope** per PRD §2 non-goals + council Option C deferral. Error classification happens in the existing poll-and-read-`failure_kind` flow.
- **Bare-ticker input:** `InstrumentAlias.alias_string` stores only the canonical dotted form (`AAPL.NASDAQ`, `ESM6.CME`) per `service.py:_upsert_definition_and_alias`. A bare input like `"AAPL"` must resolve via `registry.find_by_raw_symbol()` then pick the active alias. `lookup_for_live` must branch on `"." in sym`.
- **Overlapping alias windows:** PRD §4 US-003 edge case requires "pick the one with the most recent `effective_from`" + WARN. Manual walk over `idef.aliases` must sort by `effective_from DESC` and filter by `provider` (the `aliases` relationship returns ALL providers).
- **Subprocess pickle safety:** `TradingNodePayload` is picklable across `mp.get_context("spawn")`. Adding `resolved_instruments: tuple[ResolvedInstrument, ...]` requires a pickle round-trip test (`AssetClass(str, Enum)` pickles by name; `contract_spec: dict[str, str]` with only primitives is safe).
- **Test fixtures:** the repo's integration-test pattern is **per-module** `session_factory` + `isolated_postgres_url` (see `backend/tests/integration/test_security_master_resolve_live.py:38-54`). Root `conftest.py` only exposes `client`, `postgres_url`, `redis_url`. Do NOT use `session` / `auth_headers` as if they're shared fixtures — they aren't.

---

## Task 1: Create ResolvedInstrument + AssetClass + exceptions (foundation types)

**Files:**

- Create: `backend/src/msai/services/nautilus/security_master/live_resolver.py`
- Test: `backend/tests/unit/services/nautilus/security_master/test_live_resolver_types.py`

**Step 1: Write the failing test**

```python
# backend/tests/unit/services/nautilus/security_master/test_live_resolver_types.py
from datetime import date

import pytest

from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    RegistryIncompleteError,
    RegistryMissError,
    ResolvedInstrument,
    UnsupportedAssetClassError,
)


def test_asset_class_enum_covers_required_classes():
    assert AssetClass.EQUITY.value == "equity"
    assert AssetClass.FUTURES.value == "futures"
    assert AssetClass.FX.value == "fx"
    assert AssetClass.OPTION.value == "option"
    assert AssetClass.CRYPTO.value == "crypto"


def test_resolved_instrument_is_frozen_dataclass():
    ri = ResolvedInstrument(
        canonical_id="AAPL.NASDAQ",
        asset_class=AssetClass.EQUITY,
        contract_spec={"secType": "STK", "symbol": "AAPL"},
        effective_window=(date(2026, 1, 1), None),
    )
    with pytest.raises((AttributeError, TypeError)):
        ri.canonical_id = "other"  # type: ignore[misc]


def test_registry_miss_error_lists_symbols():
    err = RegistryMissError(symbols=["GBP/USD", "NQ"], as_of_date=date(2026, 4, 20))
    assert "GBP/USD" in str(err)
    assert "NQ" in str(err)
    assert "msai instruments refresh" in str(err)


def test_registry_incomplete_error_names_missing_field():
    err = RegistryIncompleteError(symbol="NVDA", missing_field="listing_venue")
    assert "NVDA" in str(err)
    assert "listing_venue" in str(err)


def test_unsupported_asset_class_error_names_class():
    err = UnsupportedAssetClassError(symbol="SPY_CALL_500", asset_class=AssetClass.OPTION)
    assert "option" in str(err).lower()
    assert "SPY_CALL_500" in str(err)
```

**Step 2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_types.py -v
```

Expected: `ImportError: cannot import name 'AssetClass' from 'msai.services.nautilus.security_master.live_resolver'` (file doesn't exist yet).

**Step 3: Write minimal implementation**

```python
# backend/src/msai/services/nautilus/security_master/live_resolver.py
"""Pure-read live-start instrument resolver.

Council verdict 2026-04-19 (docs/decisions/live-path-registry-wiring.md)
mandates: registry-only, no IB qualifier, no upserts. Cold-miss is operator
action (`msai instruments refresh`). This module is the runtime entrypoint
for `/api/v1/live/start-portfolio` → supervisor → IB preload.

Extending to options requires adding option-specific fields to
``contract_spec`` (expiry, strike, right) — the resolver signature and
``ResolvedInstrument`` shape do NOT change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    EQUITY = "equity"
    FUTURES = "futures"
    FX = "fx"
    OPTION = "option"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class ResolvedInstrument:
    """Result of a successful ``lookup_for_live`` resolution.

    ``contract_spec`` is opaque to the supervisor — the IB preload builder
    parses it into an ``IBContract``. Options extension adds new keys to
    ``contract_spec`` without changing this dataclass's shape.
    """

    canonical_id: str
    asset_class: AssetClass
    contract_spec: dict[str, Any]
    effective_window: tuple[date, date | None]


class LiveResolverError(ValueError):
    """Base for typed errors from :func:`lookup_for_live`.

    Subclasses ``ValueError`` so the supervisor's payload-factory
    catch in ``ProcessManager.spawn()`` treats resolver failures as
    permanent (no XAUTOCLAIM retry). ``ProcessManager`` dispatches on
    exception type before calling ``_mark_failed`` so each subclass
    maps to a distinct :class:`FailureKind`.

    Every subclass exposes :meth:`to_error_message` which returns a
    JSON string round-trippable to structured details at the API
    boundary (``EndpointOutcome.registry_permanent_failure``). Format:
    ``'{"code": "<CODE>", "message": "...", "details": {...}}'``.
    """

    def to_error_message(self) -> str:  # overridden by subclasses
        import json
        return json.dumps({"code": "LIVE_RESOLVER_ERROR", "message": str(self), "details": {}})


class RegistryMissError(LiveResolverError):
    """Raised when one or more symbols have no active registry alias.

    Error message includes a copy-pastable ``msai instruments refresh``
    command so the operator can self-correct in seconds.
    """

    def __init__(self, symbols: list[str], as_of_date: date) -> None:
        self.symbols = symbols
        self.as_of_date = as_of_date
        joined = ",".join(symbols)
        super().__init__(
            f"Symbol(s) not in registry: {symbols!r} as of {as_of_date.isoformat()}. "
            f"Run: msai instruments refresh --symbols {joined} "
            "--provider interactive_brokers"
        )

    def to_error_message(self) -> str:
        import json
        return json.dumps({
            "code": "REGISTRY_MISS",
            "message": str(self),
            "details": {
                "missing_symbols": self.symbols,
                "as_of_date": self.as_of_date.isoformat(),
            },
        })


class RegistryIncompleteError(LiveResolverError):
    """Raised when a registry row is missing a required field."""

    def __init__(self, symbol: str, missing_field: str) -> None:
        self.symbol = symbol
        self.missing_field = missing_field
        super().__init__(
            f"Registry row for {symbol!r} is incomplete: missing {missing_field!r}. "
            "This is a data-integrity issue — re-run `msai instruments refresh`."
        )

    def to_error_message(self) -> str:
        import json
        return json.dumps({
            "code": "REGISTRY_INCOMPLETE",
            "message": str(self),
            "details": {"symbol": self.symbol, "missing_field": self.missing_field},
        })


class UnsupportedAssetClassError(LiveResolverError):
    """Raised when the resolved asset_class is not wired for live trading yet."""

    def __init__(self, symbol: str, asset_class: AssetClass) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        super().__init__(
            f"Symbol {symbol!r} resolved to asset_class={asset_class.value!r} "
            "which is not yet supported for live trading. Supported: equity, futures, fx."
        )

    def to_error_message(self) -> str:
        import json
        return json.dumps({
            "code": "UNSUPPORTED_ASSET_CLASS",
            "message": str(self),
            "details": {"symbol": self.symbol, "asset_class": self.asset_class.value},
        })


class AmbiguousRegistryError(LiveResolverError):
    """Raised when the resolver cannot deterministically pick a single
    registry row/alias for a symbol. Two sources:

    1. **Cross-asset-class:** a bare symbol matches multiple
       ``instrument_definitions`` rows across asset_classes (e.g. SPY
       as equity AND option underlying). Wraps the registry-layer
       ``AmbiguousSymbolError`` so it flows through the ``ValueError``
       permanent-catch (instead of the transient-retry branch that
       would catch a bare ``AmbiguousSymbolError``).

    2. **Same-day overlap:** multiple active aliases share the same
       (maximum) ``effective_from`` date — operator-seeded data-
       integrity issue; no deterministic PRD tie-break rule applies.

    Consumers differentiate via ``reason`` attribute.
    """

    REASON_CROSS_ASSET_CLASS = "cross_asset_class"
    REASON_SAME_DAY_OVERLAP = "same_day_overlap"

    def __init__(
        self,
        symbol: str,
        conflicts: list[str],
        reason: str,
    ) -> None:
        self.symbol = symbol
        self.conflicts = sorted(conflicts)
        self.reason = reason
        if reason == self.REASON_CROSS_ASSET_CLASS:
            msg = (
                f"Symbol {symbol!r} matches multiple registry definitions "
                f"across asset_classes {self.conflicts!r}; pin the "
                "asset_class by passing the dotted alias form (e.g. 'SPY.ARCA')."
            )
        else:
            msg = (
                f"Symbol {symbol!r} has multiple active aliases on the same "
                f"effective_from date: {self.conflicts!r}. Operator must "
                "close one alias row — re-run `msai instruments refresh` "
                "or manually set effective_to on the stale row."
            )
        super().__init__(msg)

    def to_error_message(self) -> str:
        import json
        return json.dumps({
            "code": "AMBIGUOUS_REGISTRY",
            "message": str(self),
            "details": {
                "symbol": self.symbol,
                "reason": self.reason,
                "conflicts": self.conflicts,
            },
        })
```

**Step 4: Run test to verify it passes**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_types.py -v
```

Expected: PASS (5/5).

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/live_resolver.py \
        backend/tests/unit/services/nautilus/security_master/test_live_resolver_types.py
git commit -m "feat(security_master): add ResolvedInstrument + typed live-resolver errors

- New module live_resolver.py with AssetClass enum, ResolvedInstrument
  frozen dataclass, and RegistryMissError / RegistryIncompleteError /
  UnsupportedAssetClassError
- Options-extensible contract_spec dict per council verdict 2026-04-19
- Error messages include copy-pastable operator hints

Refs: docs/decisions/live-path-registry-wiring.md"
```

---

## Task 2: Contract spec construction helpers (per asset class)

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/live_resolver.py`
- Test: `backend/tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py`

**Step 1: Write the failing tests**

```python
# backend/tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py
from datetime import date

import pytest

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    RegistryIncompleteError,
    _build_contract_spec,
)


def _make_definition(**overrides) -> InstrumentDefinition:
    base = dict(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        provider="interactive_brokers",
    )
    base.update(overrides)
    return InstrumentDefinition(**base)


def _make_alias(alias_string: str, effective_from: date) -> InstrumentAlias:
    return InstrumentAlias(
        alias_string=alias_string,
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=effective_from,
    )


def test_equity_contract_spec():
    spec = _build_contract_spec(
        _make_definition(),
        _make_alias("AAPL.NASDAQ", date(2026, 1, 1)),
    )
    assert spec == {
        "secType": "STK",
        "symbol": "AAPL",
        "exchange": "SMART",
        "primaryExchange": "NASDAQ",
        "currency": "USD",
    }


def test_etf_contract_spec_uses_arca_primary():
    spec = _build_contract_spec(
        _make_definition(raw_symbol="SPY", listing_venue="ARCA"),
        _make_alias("SPY.ARCA", date(2026, 1, 1)),
    )
    assert spec["primaryExchange"] == "ARCA"
    assert spec["secType"] == "STK"


def test_fx_contract_spec():
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="EUR/USD",
            listing_venue="IDEALPRO",
            routing_venue="IDEALPRO",
            asset_class="fx",
        ),
        _make_alias("EUR/USD.IDEALPRO", date(2026, 1, 1)),
    )
    assert spec == {
        "secType": "CASH",
        "symbol": "EUR",
        "exchange": "IDEALPRO",
        "currency": "USD",
    }


def test_futures_contract_spec_parses_alias_string():
    """ES alias 'ESM6.CME' -> lastTradeDateOrContractMonth='202606'."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESM6.CME", date(2026, 3, 20)),
    )
    assert spec == {
        "secType": "FUT",
        "symbol": "ES",
        "exchange": "CME",
        "lastTradeDateOrContractMonth": "202606",
        "currency": "USD",
    }


def test_futures_contract_spec_z5_december_2025():
    """Z = December, 5 = 2025 (assuming century context inferred)."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="NQ",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("NQZ5.CME", date(2025, 9, 1)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "202512"


def test_futures_decade_boundary_forward():
    """effective_from=2029-12-15 + alias ESH0.CME → March 2030, not 2020."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESH0.CME", date(2029, 12, 15)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "203003"


def test_futures_in_decade_uses_current_year_not_next():
    """effective_from=2026-01-01 + alias ESM6.CME → 2026-06, not 2036."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESM6.CME", date(2026, 1, 1)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "202606"


def test_equity_missing_listing_venue_raises_incomplete():
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(listing_venue=""),
            _make_alias("AAPL.NASDAQ", date(2026, 1, 1)),
        )
    assert excinfo.value.missing_field == "listing_venue"


def test_fx_raw_symbol_without_slash_raises_incomplete():
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(raw_symbol="EURUSD", asset_class="fx"),
            _make_alias("EURUSD.IDEALPRO", date(2026, 1, 1)),
        )
    assert excinfo.value.missing_field == "raw_symbol.base_quote_split"


def test_futures_malformed_alias_raises_incomplete():
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(raw_symbol="ES", asset_class="futures"),
            _make_alias("ES.CME", date(2026, 1, 1)),  # missing month code
        )
    assert "alias" in excinfo.value.missing_field
```

**Step 2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py -v
```

Expected: `ImportError: cannot import name '_build_contract_spec'`.

**Step 3: Write minimal implementation**

Append to `backend/src/msai/services/nautilus/security_master/live_resolver.py`:

```python
# --- contract_spec construction ----------------------------------------
# IB futures month codes: F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun,
# N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec.
_FUTURES_MONTH_CODES = {
    "F": "01", "G": "02", "H": "03", "J": "04", "K": "05", "M": "06",
    "N": "07", "Q": "08", "U": "09", "V": "10", "X": "11", "Z": "12",
}


def _build_contract_spec(
    definition: "InstrumentDefinition",  # type: ignore[name-defined]
    alias: "InstrumentAlias",  # type: ignore[name-defined]
) -> dict[str, Any]:
    """Construct an IB-compatible contract_spec from a registry row pair.

    Returned dict is consumed by ``build_ib_instrument_provider_config`` to
    reconstruct an ``IBContract``. Fields are IB SDK conventions (secType,
    symbol, exchange, primaryExchange, currency, lastTradeDateOrContractMonth).
    """
    if not definition.listing_venue:
        raise RegistryIncompleteError(definition.raw_symbol, "listing_venue")
    if not definition.routing_venue:
        raise RegistryIncompleteError(definition.raw_symbol, "routing_venue")

    ac = definition.asset_class
    if ac == AssetClass.EQUITY.value:
        return {
            "secType": "STK",
            "symbol": definition.raw_symbol,
            "exchange": definition.routing_venue,
            "primaryExchange": definition.listing_venue,
            "currency": "USD",
        }
    if ac == AssetClass.FX.value:
        if "/" not in definition.raw_symbol:
            raise RegistryIncompleteError(
                definition.raw_symbol, "raw_symbol.base_quote_split"
            )
        base, quote = definition.raw_symbol.split("/", 1)
        if not base or not quote:
            raise RegistryIncompleteError(
                definition.raw_symbol, "raw_symbol.malformed"
            )
        return {
            "secType": "CASH",
            "symbol": base,
            "exchange": definition.routing_venue,
            "currency": quote,
        }
    if ac == AssetClass.FUTURES.value:
        return {
            "secType": "FUT",
            "symbol": definition.raw_symbol,
            "exchange": definition.routing_venue,
            "lastTradeDateOrContractMonth": _parse_futures_expiry(
                alias.alias_string, alias.effective_from, definition.raw_symbol
            ),
            "currency": "USD",
        }
    # Option / crypto are raised at the lookup_for_live boundary, not here;
    # this branch is defensive.
    raise RegistryIncompleteError(definition.raw_symbol, f"asset_class={ac}")


def _parse_futures_expiry(
    alias_string: str,
    effective_from: date,
    raw_symbol: str,
) -> str:
    """Parse ``ESM6.CME`` → ``'202606'``.

    Year disambiguation: pick the smallest year >= effective_from.year
    whose units digit matches ``year_digit``. This correctly handles
    decade-boundary rolls (effective_from=2029-12-15 + alias ``ESH0``
    → 2030-03, not 2020-03).
    """
    root, _, _venue = alias_string.partition(".")
    # Strip the root symbol prefix (e.g. "ES" or "NQ") to isolate the
    # "M6" / "Z5" tail.
    if not root.startswith(raw_symbol):
        raise RegistryIncompleteError(
            raw_symbol, f"alias.root_mismatch: {alias_string!r}"
        )
    tail = root[len(raw_symbol) :]
    if len(tail) != 2 or tail[0] not in _FUTURES_MONTH_CODES:
        raise RegistryIncompleteError(
            raw_symbol, f"alias.month_code: {alias_string!r}"
        )
    month_code, year_digit_str = tail[0], tail[1]
    if not year_digit_str.isdigit():
        raise RegistryIncompleteError(
            raw_symbol, f"alias.year_digit: {alias_string!r}"
        )
    year_digit = int(year_digit_str)
    base = effective_from.year
    base_decade = (base // 10) * 10
    candidate = base_decade + year_digit
    # If the in-decade candidate is already in the past relative to the
    # alias becoming active, the expiry is next decade.
    if candidate < base:
        candidate += 10
    return f"{candidate:04d}{_FUTURES_MONTH_CODES[month_code]}"
```

**Step 4: Run test to verify it passes**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py -v
```

Expected: PASS (8/8).

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/live_resolver.py \
        backend/tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py
git commit -m "feat(security_master): add contract_spec builders for equity/ETF/FX/futures

- _build_contract_spec derives IB-compatible dict from registry rows
- Futures month-code parsing (ESM6.CME → 202606) uses effective_from
  year to disambiguate decade; defensive on malformed aliases
- FX splits raw_symbol on '/' into base/quote; enforces presence
- Raises RegistryIncompleteError with specific missing_field on corruption

Refs: docs/decisions/live-path-registry-wiring.md constraint #2"
```

---

## Task 3: lookup_for_live happy path (single equity, registry-seeded)

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/live_resolver.py`
- Test: `backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py`

**Step 1: Write the failing test**

```python
# backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py
"""Integration tests for lookup_for_live.

Follows the repo's per-module testcontainer pattern (see
test_security_master_resolve_live.py:38-54) — a fresh Postgres
container per module, async_sessionmaker on top, each test opens
its own session.
"""
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
    lookup_for_live,
)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture(scope="module")
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s
        # Cleanup — drop seeded rows between tests.
        from sqlalchemy import delete

        await s.execute(delete(InstrumentAlias))
        await s.execute(delete(InstrumentDefinition))
        await s.commit()


async def _seed_aapl(session: AsyncSession) -> InstrumentDefinition:
    d = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        provider="interactive_brokers",
    )
    session.add(d)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=d.instrument_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()
    return d


async def test_lookup_bare_ticker_returns_resolved_instrument(session):
    """Bare input 'AAPL' must resolve via find_by_raw_symbol."""
    await _seed_aapl(session)

    result = await lookup_for_live(
        ["AAPL"],
        as_of_date=date(2026, 4, 20),
        session=session,
    )

    assert len(result) == 1
    ri = result[0]
    assert isinstance(ri, ResolvedInstrument)
    assert ri.canonical_id == "AAPL.NASDAQ"
    assert ri.asset_class == AssetClass.EQUITY
    assert ri.contract_spec["secType"] == "STK"
    assert ri.contract_spec["primaryExchange"] == "NASDAQ"


async def test_lookup_dotted_alias_returns_resolved_instrument(session):
    """Dotted input 'AAPL.NASDAQ' must resolve via find_by_alias."""
    await _seed_aapl(session)

    result = await lookup_for_live(
        ["AAPL.NASDAQ"],
        as_of_date=date(2026, 4, 20),
        session=session,
    )

    assert len(result) == 1
    assert result[0].canonical_id == "AAPL.NASDAQ"


async def test_lookup_empty_symbols_raises_value_error(session):
    with pytest.raises(ValueError, match="empty"):
        await lookup_for_live([], as_of_date=date(2026, 4, 20), session=session)


async def test_lookup_requires_as_of_date_not_datetime(session):
    from datetime import UTC, datetime

    with pytest.raises(TypeError, match="date"):
        await lookup_for_live(
            ["AAPL"],
            as_of_date=datetime.now(UTC),  # type: ignore[arg-type]
            session=session,
        )
```

Note: `pytest-asyncio` mode is `auto` per `backend/pyproject.toml:67`, so `async def test_*` functions don't need the `@pytest.mark.asyncio` decorator. Seed helpers are module-local per project convention.

**Step 2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/integration/services/nautilus/security_master/test_lookup_for_live.py::test_lookup_single_equity_returns_resolved_instrument -v
```

Expected: `ImportError: cannot import name 'lookup_for_live'`.

**Step 3: Write minimal implementation**

Append to `backend/src/msai/services/nautilus/security_master/live_resolver.py`:

```python
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.registry import InstrumentRegistry

_log = structlog.get_logger(__name__)


def _pick_active_alias(
    idef: InstrumentDefinition,
    *,
    provider: str,
    as_of_date: date,
    caller_symbol: str,
) -> InstrumentAlias | None:
    """Pick the active alias for ``idef`` under ``provider`` on ``as_of_date``.

    Filters by provider (the relationship loads ALL providers) and by
    effective window. On window overlap, returns the alias with the
    most recent ``effective_from`` (PRD §4 US-003 tie-break rule).

    Callers should log a WARN when ``len(candidates) > 1`` — operator
    seeded overlapping rows that need cleanup.
    """
    candidates = [
        a
        for a in idef.aliases
        if a.provider == provider
        and a.effective_from <= as_of_date
        and (a.effective_to is None or a.effective_to > as_of_date)
    ]
    if not candidates:
        return None
    # Sort by effective_from DESC. If multiple active aliases share
    # the max effective_from, that's an operator-seeded data-integrity
    # issue — do NOT silently pick one, raise AmbiguousRegistryError.
    # (PRD §4 US-003 authorizes "pick the most recent effective_from";
    # when max isn't unique, the PRD's tie-break rule doesn't apply.)
    candidates.sort(key=lambda a: a.effective_from, reverse=True)
    max_date = candidates[0].effective_from
    tied_at_max = [c for c in candidates if c.effective_from == max_date]
    if len(tied_at_max) > 1:
        tied_at_max.sort(key=lambda a: a.alias_string)  # deterministic reporting
        raise AmbiguousRegistryError(
            # Use caller-facing symbol (raw ticker from caller input)
            # so the error matches the request, not the alias's month
            # suffix. idef.raw_symbol would also work but caller_symbol
            # is what the operator typed.
            symbol=caller_symbol,
            conflicts=[a.alias_string for a in tied_at_max],
            reason=AmbiguousRegistryError.REASON_SAME_DAY_OVERLAP,
        )
    return tied_at_max[0]


async def lookup_for_live(
    symbols: list[str],
    *,
    as_of_date: date,
    session: AsyncSession,
    provider: str = "interactive_brokers",
) -> list[ResolvedInstrument]:
    """Pure-read registry resolver for the live-start critical path.

    Args:
        symbols: Non-empty list of raw tickers or dotted aliases.
            Dotted inputs (``"AAPL.NASDAQ"``) are looked up via
            :meth:`InstrumentRegistry.find_by_alias`; bare inputs
            (``"AAPL"``) via :meth:`find_by_raw_symbol`. See
            ``service.py:272-296`` for the precedent this mirrors.
        as_of_date: Exchange-local date (America/Chicago). Required —
            no default, because ``find_by_alias`` defaults to UTC
            which regresses roll-day behavior.
        session: Async DB session.
        provider: Registry provider namespace (default
            ``"interactive_brokers"`` — the only one wired for live).

    Returns:
        ``ResolvedInstrument`` per input symbol, order-preserved.

    Raises:
        ValueError: ``symbols`` is empty.
        TypeError: ``as_of_date`` is not a :class:`datetime.date`.
        RegistryMissError: One or more symbols have no active alias.
        RegistryIncompleteError: A matched row is missing required fields.
        UnsupportedAssetClassError: A matched row has asset_class
            option/crypto (not wired for live yet).
    """
    if not symbols:
        raise ValueError("symbols cannot be empty")
    # Reject datetime explicitly — ``isinstance(datetime, date)`` is True
    # because datetime subclasses date, so we check the reverse.
    from datetime import datetime as _dt
    if isinstance(as_of_date, _dt) or not isinstance(as_of_date, date):
        raise TypeError(
            "as_of_date must be a datetime.date in America/Chicago "
            "semantics, not a datetime. "
            "Use exchange_local_today() or date.fromisoformat(spawn_today_iso)."
        )

    registry = InstrumentRegistry(session)
    resolved: list[ResolvedInstrument] = []
    missing: list[str] = []

    for sym in symbols:
        # Dotted vs bare branching — alias_string always stores the
        # canonical dotted form (service.py:_upsert_definition_and_alias).
        if "." in sym:
            idef = await registry.find_by_alias(
                sym, provider=provider, as_of_date=as_of_date
            )
        else:
            # Bare ticker — resolve via raw_symbol lookup. The registry
            # raises AmbiguousSymbolError (NOT a ValueError) on cross-
            # asset-class match; we wrap it into AmbiguousRegistryError
            # (subclass of LiveResolverError → ValueError) so the
            # supervisor's permanent-catch fires instead of the
            # transient-retry branch.
            from msai.services.nautilus.security_master.registry import (
                AmbiguousSymbolError,
            )
            try:
                idef = await registry.find_by_raw_symbol(
                    sym, provider=provider, asset_class=None
                )
            except AmbiguousSymbolError as exc:
                # AmbiguousSymbolError exposes `.asset_classes: list[str]`
                # as an attribute (added in registry.py in this PR's
                # small registry enhancement — see Task 3b). We wrap
                # into AmbiguousRegistryError so the supervisor's
                # permanent-catch fires (AmbiguousSymbolError alone is
                # an Exception, not a ValueError).
                raise AmbiguousRegistryError(
                    symbol=sym,
                    conflicts=exc.asset_classes,
                    reason=AmbiguousRegistryError.REASON_CROSS_ASSET_CLASS,
                ) from exc
        if idef is None:
            missing.append(sym)
            continue

        # Validate asset class. Unknown value at the DB layer (schema
        # CHECK constraint normally prevents this) → RegistryIncomplete
        # with telemetry emission before raise.
        try:
            ac = AssetClass(idef.asset_class)
        except ValueError as e:
            _log.error(
                "live_instrument_resolved",
                event="live_instrument_resolved",
                source="registry_incomplete",
                symbol=sym,
                missing_field=f"asset_class={idef.asset_class!r}",
                as_of_date=as_of_date.isoformat(),
            )
            LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
                source="registry_incomplete", asset_class="unknown",
            ).inc()
            raise RegistryIncompleteError(
                sym, f"asset_class={idef.asset_class!r}",
            ) from e
        if ac in (AssetClass.OPTION, AssetClass.CRYPTO):
            raise UnsupportedAssetClassError(sym, ac)

        # Pick active alias (provider-filtered + overlap-deterministic)
        active_alias = _pick_active_alias(
            idef, provider=provider, as_of_date=as_of_date,
            caller_symbol=sym,
        )
        if active_alias is None:
            missing.append(sym)
            continue

        # Overlap WARN (PRD §4 US-003 edge case)
        overlap_count = sum(
            1
            for a in idef.aliases
            if a.provider == provider
            and a.effective_from <= as_of_date
            and (a.effective_to is None or a.effective_to > as_of_date)
        )
        if overlap_count > 1:
            _log.warning(
                "live_instrument_alias_overlap",
                symbol=sym,
                as_of_date=as_of_date.isoformat(),
                overlap_count=overlap_count,
                chosen=active_alias.alias_string,
                note="operator seeded overlapping rows; resolver picked newest effective_from",
            )

        spec = _build_contract_spec(idef, active_alias)
        resolved.append(
            ResolvedInstrument(
                canonical_id=active_alias.alias_string,
                asset_class=ac,
                contract_spec=spec,
                effective_window=(active_alias.effective_from, active_alias.effective_to),
            )
        )

    if missing:
        raise RegistryMissError(symbols=missing, as_of_date=as_of_date)

    return resolved
```

**Step 4: Run test to verify it passes**

```bash
cd backend && uv run pytest tests/integration/services/nautilus/security_master/test_lookup_for_live.py -v
```

Expected: 3/3 PASS.

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/live_resolver.py \
        backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py
git commit -m "feat(security_master): add lookup_for_live resolver (single-equity happy path)

- Pure-read; calls InstrumentRegistry.find_by_alias with explicit as_of_date
- Rejects empty symbols (ValueError) and datetime args (TypeError)
- Returns order-preserved ResolvedInstrument list; raises typed errors
  on miss / incomplete / unsupported asset class

Refs: docs/decisions/live-path-registry-wiring.md council verdict"
```

---

## Task 3b: Registry enhancements — remove UTC default + expose asset_classes

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/registry.py`
- Modify: `backend/src/msai/services/nautilus/security_master/service.py` (thread `as_of_date` through the one existing caller: `resolve_for_backtest` → `find_by_alias`)
- Test: extend existing `backend/tests/integration/test_security_master_resolve_live.py` with a signature-lock test

**Change 1: `find_by_alias(as_of_date)` becomes required — complete caller audit**

Today at `registry.py:52-60`:

```python
async def find_by_alias(
    self,
    alias_string: str,
    *,
    provider: str,
    as_of_date: date | None = None,  # <-- DEFAULT IS UTC TODAY
) -> InstrumentDefinition | None:
    as_of = as_of_date or datetime.now(UTC).date()
    ...
```

Iter-1 research brief + iter-2 review: the UTC default regresses futures-roll correctness if any caller forgets to pass Chicago-local `spawn_today`. Make it required:

```python
async def find_by_alias(
    self,
    alias_string: str,
    *,
    provider: str,
    as_of_date: date,  # required — no default
) -> InstrumentDefinition | None:
    ...
```

**Complete caller audit (verified 2026-04-20 via grep):**

1. `service.py:268` — `resolve_for_live`'s dotted-alias warm-path today calls `registry.find_by_alias(sym, provider="interactive_brokers")` WITHOUT `as_of_date`. `today` is already computed at line 263 via `exchange_local_today()` — thread it in: `registry.find_by_alias(sym, provider="interactive_brokers", as_of_date=today)`.
2. `service.py:~395-431` — `resolve_for_backtest`'s dotted-alias path (PR #32's fix). Already passes `as_of_date`. No change.
3. `registry.py:119-128` — `InstrumentRegistry.require_definition` wrapper also has `as_of_date: date | None = None`. Remove this default too, and pass `as_of_date` through unmodified to the `find_by_alias` call at line 126-128.
4. `lookup_for_live` (Task 3, new code) — already passes `as_of_date`. No change.
5. Test files (verified: `backend/tests/integration/test_instrument_registry.py:150` currently calls `require_definition` without `as_of_date` in a miss-test). Update to pass a concrete `date(2026, 4, 20)` or similar fixed test date.

Grep command to re-verify before merge:

```bash
rg "find_by_alias\(|require_definition\(" backend/src backend/tests | grep -v "as_of_date"
```

Expected post-fix: zero matches (every caller explicitly passes `as_of_date`).

**Change 2: `AmbiguousSymbolError` carries structured `asset_classes`**

Today at `registry.py:31-40`:

```python
class AmbiguousSymbolError(Exception):
    """Raised when a raw symbol matches multiple definitions..."""
```

Add an explicit constructor so `lookup_for_live` can read the list without string parsing:

```python
class AmbiguousSymbolError(Exception):
    def __init__(self, symbol: str, provider: str, asset_classes: list[str]) -> None:
        self.symbol = symbol
        self.provider = provider
        self.asset_classes = asset_classes
        super().__init__(
            f"Symbol {symbol!r} matches {len(asset_classes)} definitions under "
            f"provider {provider!r} across asset_classes {sorted(asset_classes)}; "
            "specify asset_class explicitly."
        )
```

Update the raise site at `registry.py:110-116` inside `find_by_raw_symbol`:

```python
# BEFORE (registry.py:110-116)
if len(rows) > 1:
    classes = sorted({r.asset_class for r in rows})
    raise AmbiguousSymbolError(
        f"Symbol {raw_symbol!r} matches {len(rows)} definitions under "
        f"provider {provider!r} across asset_classes {classes}; "
        "specify asset_class explicitly."
    )

# AFTER
if len(rows) > 1:
    classes = sorted({r.asset_class for r in rows})
    raise AmbiguousSymbolError(
        symbol=raw_symbol,
        provider=provider,
        asset_classes=classes,
    )
```

Existing integration test at `backend/tests/integration/test_instrument_registry.py:177` uses `pytest.raises(AmbiguousSymbolError, match="SPY")` — still passes because the new `__init__` embeds `symbol={symbol!r}` in `super().__init__(...)` message.

**Step 1: Failing test — signature lock**

```python
# append to backend/tests/integration/test_security_master_resolve_live.py
import inspect

from msai.services.nautilus.security_master.registry import (
    AmbiguousSymbolError,
    InstrumentRegistry,
)


def test_find_by_alias_requires_as_of_date():
    sig = inspect.signature(InstrumentRegistry.find_by_alias)
    param = sig.parameters["as_of_date"]
    assert param.default is inspect.Parameter.empty, (
        "as_of_date must be required — UTC default regresses roll-day behavior"
    )


def test_ambiguous_symbol_error_exposes_asset_classes():
    err = AmbiguousSymbolError(
        symbol="SPY", provider="interactive_brokers",
        asset_classes=["equity", "option"],
    )
    assert err.asset_classes == ["equity", "option"]
    assert err.symbol == "SPY"
```

**Step 2: Run — FAIL (default still there; attribute missing).**

**Step 3: Implement** — two small edits in `registry.py` as described above; update `service.py:resolve_for_backtest` to pass `as_of_date` (it already computes one — confirm at lines ~380-431).

**Step 4: Run — PASS.**

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/registry.py \
        backend/src/msai/services/nautilus/security_master/service.py \
        backend/tests/integration/test_security_master_resolve_live.py
git commit -m "fix(security_master): remove find_by_alias UTC default + structured AmbiguousSymbolError

- as_of_date is now required on find_by_alias — prevents silent
  UTC regression on roll days if a future caller forgets to pass it
- AmbiguousSymbolError now carries symbol/provider/asset_classes as
  attributes so wrapping code (lookup_for_live) doesn't parse strings
- No behavior change for existing callers; all already pass as_of_date"
```

---

## Task 4: Characterization test — multi-symbol partial-miss aggregation

**Framing:** this is NOT a TDD Red-Green-Refactor task — Task 3's implementation already aggregates missing symbols. This characterization test _locks_ that aggregation invariant so future refactors can't silently change it to short-circuit-on-first-miss. Commit alongside Task 3 or directly after.

**Files:**

- Modify: `backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py` (append)

**Step 1: Write the locking test**

```python
# append to test_lookup_for_live.py
@pytest.mark.asyncio
async def test_lookup_partial_miss_aggregates_all_missing(session):
    # Arrange — seed AAPL only
    definition = InstrumentDefinition(
        raw_symbol="AAPL", listing_venue="NASDAQ", routing_venue="SMART",
        asset_class="equity", provider="interactive_brokers",
    )
    session.add(definition)
    await session.flush()
    session.add(InstrumentAlias(
        instrument_uid=definition.instrument_uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
    ))
    await session.commit()

    # Act + Assert — request AAPL, QQQ, GBP/USD; only AAPL is in registry
    from msai.services.nautilus.security_master.live_resolver import (
        RegistryMissError,
    )
    with pytest.raises(RegistryMissError) as excinfo:
        await lookup_for_live(
            ["AAPL", "QQQ", "GBP/USD"],
            as_of_date=date(2026, 4, 20),
            session=session,
        )
    assert set(excinfo.value.symbols) == {"QQQ", "GBP/USD"}
    # AAPL must not be reported missing
    assert "AAPL" not in excinfo.value.symbols
    # Error message must contain the copy-pastable refresh command
    assert "msai instruments refresh" in str(excinfo.value)
```

**Step 2: Run to verify it fails (passes actually — already aggregates)**

Run it. If it passes on the current implementation, that's fine — TDD's purpose is to lock behavior, not to force a rewrite. Verify:

```bash
cd backend && uv run pytest tests/integration/services/nautilus/security_master/test_lookup_for_live.py::test_lookup_partial_miss_aggregates_all_missing -v
```

If it passes, proceed to Step 5. If it fails, go to Step 3 to fix.

**Step 5: Commit**

```bash
git add backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py
git commit -m "test(security_master): lock multi-symbol partial-miss aggregation

Verifies RegistryMissError lists ALL missing symbols in one raise,
never silently succeeds for partial matches."
```

---

## Task 5: Expired-alias miss + corrupt-row incomplete + unsupported asset class

**Files:**

- Modify: `backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py` (append 3 tests)

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_lookup_expired_alias_is_miss(session):
    definition = InstrumentDefinition(
        raw_symbol="ES", listing_venue="CME", routing_venue="CME",
        asset_class="futures", provider="interactive_brokers",
    )
    session.add(definition)
    await session.flush()
    # Only a historical alias, expired before 2026-04-20
    session.add(InstrumentAlias(
        instrument_uid=definition.instrument_uid,
        alias_string="ESH6.CME",  # March 2026 — expired by April
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2025, 12, 15),
        effective_to=date(2026, 3, 19),
    ))
    await session.commit()

    from msai.services.nautilus.security_master.live_resolver import (
        RegistryMissError,
    )
    with pytest.raises(RegistryMissError):
        await lookup_for_live(
            ["ES"], as_of_date=date(2026, 4, 20), session=session,
        )


async def test_lookup_propagates_incomplete_from_build_spec(
    session, monkeypatch
):
    """Corrupt row: simulate _build_contract_spec raising; lookup_for_live
    must propagate RegistryIncompleteError AND fire an ERROR alert.

    DB-level NULL is unreachable (NOT NULL constraint), so we seed a
    valid row then monkey-patch the spec builder to raise. This
    verifies the propagation + alerting path that Task 8 implements.
    """
    # Seed valid AAPL so find_by_raw_symbol succeeds.
    await _seed_aapl(session)

    from msai.services.nautilus.security_master import live_resolver
    from msai.services.nautilus.security_master.live_resolver import (
        RegistryIncompleteError,
    )

    def _raising_spec(definition, alias):
        raise RegistryIncompleteError(
            symbol=definition.raw_symbol, missing_field="listing_venue"
        )

    monkeypatch.setattr(live_resolver, "_build_contract_spec", _raising_spec)

    with pytest.raises(RegistryIncompleteError) as excinfo:
        await lookup_for_live(
            ["AAPL"], as_of_date=date(2026, 4, 20), session=session,
        )
    assert excinfo.value.missing_field == "listing_venue"


@pytest.mark.asyncio
async def test_lookup_option_asset_class_raises_unsupported(session):
    definition = InstrumentDefinition(
        raw_symbol="SPY_CALL_500_20260619",
        listing_venue="CBOE", routing_venue="SMART",
        asset_class="option", provider="interactive_brokers",
    )
    session.add(definition)
    await session.flush()
    session.add(InstrumentAlias(
        instrument_uid=definition.instrument_uid,
        alias_string="SPY_CALL_500_20260619.CBOE",
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
    ))
    await session.commit()

    from msai.services.nautilus.security_master.live_resolver import (
        UnsupportedAssetClassError,
    )
    with pytest.raises(UnsupportedAssetClassError) as excinfo:
        await lookup_for_live(
            ["SPY_CALL_500_20260619.CBOE"],
            as_of_date=date(2026, 4, 20),
            session=session,
        )
    assert excinfo.value.asset_class.value == "option"
```

**Step 2 + 4: Run tests**

```bash
cd backend && uv run pytest tests/integration/services/nautilus/security_master/test_lookup_for_live.py -v
```

Expected: 5 PASS, 1 SKIP.

**Step 5: Commit**

```bash
git add backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py
git commit -m "test(security_master): lock expired-alias miss + unsupported asset class

Expired alias (effective_to in past) is treated as miss. Option
asset_class raises UnsupportedAssetClassError at resolver boundary."
```

---

## Task 6: Futures-roll correctness (alias window threading)

**Files:**

- Modify: `backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py` (append)

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_lookup_futures_roll_pre_roll_returns_front_month(session):
    definition = InstrumentDefinition(
        raw_symbol="ES", listing_venue="CME", routing_venue="CME",
        asset_class="futures", provider="interactive_brokers",
    )
    session.add(definition)
    await session.flush()
    # June 2026 alias active until 2026-06-19
    session.add(InstrumentAlias(
        instrument_uid=definition.instrument_uid,
        alias_string="ESM6.CME",
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2026, 3, 20),
        effective_to=date(2026, 6, 20),
    ))
    # September 2026 alias takes over 2026-06-20
    session.add(InstrumentAlias(
        instrument_uid=definition.instrument_uid,
        alias_string="ESU6.CME",
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2026, 6, 20),
    ))
    await session.commit()

    # Pre-roll: 2026-06-19 -> ESM6
    result = await lookup_for_live(
        ["ES"], as_of_date=date(2026, 6, 19), session=session,
    )
    assert result[0].canonical_id == "ESM6.CME"
    assert result[0].contract_spec["lastTradeDateOrContractMonth"] == "202606"


@pytest.mark.asyncio
async def test_lookup_futures_roll_post_roll_returns_next_month(session):
    # (seed identical to previous test — factor into fixture in practice)
    # ...same seed as above...

    result = await lookup_for_live(
        ["ES"], as_of_date=date(2026, 6, 20), session=session,
    )
    assert result[0].canonical_id == "ESU6.CME"
    assert result[0].contract_spec["lastTradeDateOrContractMonth"] == "202609"
```

**Step 2 + 4: Run to verify**

```bash
cd backend && uv run pytest tests/integration/services/nautilus/security_master/test_lookup_for_live.py -v -k roll
```

Expected: 2 PASS.

**Step 5: Commit**

```bash
git add backend/tests/integration/services/nautilus/security_master/test_lookup_for_live.py
git commit -m "test(security_master): lock futures-roll semantics (ESM6 -> ESU6 boundary)

as_of_date=2026-06-19 returns ESM6 (June front-month);
as_of_date=2026-06-20 returns ESU6 (September). The resolver's
as_of_date threads directly into InstrumentRegistry.find_by_alias's
effective-window query — no logic duplication."
```

---

## Task 7: Structured telemetry — log + counter (project's hand-rolled registry)

**Files:**

- Modify: `backend/src/msai/services/observability/trading_metrics.py` (register counter via `_r.counter(...)` — **NO `prometheus_client` import**; project has a hand-rolled `MetricsRegistry` at `services/observability/metrics.py`)
- Modify: `backend/src/msai/services/nautilus/security_master/live_resolver.py` (emit log + increment)
- Test: `backend/tests/unit/services/nautilus/security_master/test_live_resolver_telemetry.py`

**Step 0: Confirm existing pattern**

Read `backend/src/msai/services/observability/trading_metrics.py:1-55`. Every counter registers as:

```python
_r = get_registry()
DEPLOYMENTS_STARTED = _r.counter("msai_deployments_started_total", "Live deployments started")
```

No `prometheus_client` anywhere in `backend/src/`. The `/metrics` endpoint at `main.py:270-286` renders via `get_registry().render()`. Any new counter MUST follow this pattern or it won't be exposed.

The `Counter` class supports `.labels(**kwargs).inc()` (see `metrics.py:116-138`) — label keys are free-form strings applied at increment time.

**Step 1: Write the failing test**

```python
# backend/tests/unit/services/nautilus/security_master/test_live_resolver_telemetry.py
import logging
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
    lookup_for_live,
)


@pytest.mark.asyncio
async def test_successful_resolution_emits_structured_log(caplog, session_with_aapl):
    caplog.set_level(logging.INFO, logger="msai.services.nautilus.security_master.live_resolver")
    await lookup_for_live(
        ["AAPL"], as_of_date=date(2026, 4, 20), session=session_with_aapl
    )
    # Find the structured event
    events = [
        r for r in caplog.records
        if getattr(r, "event", None) == "live_instrument_resolved"
    ]
    assert len(events) == 1
    evt = events[0]
    assert evt.source == "registry"
    assert evt.symbol == "AAPL"
    assert evt.canonical_id == "AAPL.NASDAQ"
    assert evt.asset_class == "equity"
    assert evt.as_of_date == "2026-04-20"


@pytest.mark.asyncio
async def test_registry_miss_emits_structured_log(caplog, session):
    caplog.set_level(logging.INFO, logger="msai.services.nautilus.security_master.live_resolver")
    from msai.services.nautilus.security_master.live_resolver import (
        RegistryMissError,
    )
    with pytest.raises(RegistryMissError):
        await lookup_for_live(
            ["UNKNOWN"], as_of_date=date(2026, 4, 20), session=session
        )
    events = [
        r for r in caplog.records
        if getattr(r, "event", None) == "live_instrument_resolved"
    ]
    assert len(events) == 1
    assert events[0].source == "registry_miss"


def test_counter_is_registered():
    # Registry introspection only — do NOT .inc() on the shared
    # process-global counter; that state would pollute other tests
    # that snapshot counter values.
    from msai.services.observability import get_registry
    from msai.services.observability.metrics import Counter
    from msai.services.observability.trading_metrics import (
        LIVE_INSTRUMENT_RESOLVED_TOTAL,
    )

    registry = get_registry()
    # Name is registered in the shared MetricsRegistry.
    assert "msai_live_instrument_resolved_total" in registry._metrics
    # And it's a Counter, not a Gauge, so it supports .labels(**kwargs).inc().
    assert isinstance(
        registry._metrics["msai_live_instrument_resolved_total"], Counter
    )
    # The module-level reference is the same instance (idempotent factory).
    assert LIVE_INSTRUMENT_RESOLVED_TOTAL is registry._metrics[
        "msai_live_instrument_resolved_total"
    ]
```

**Fixture:** This test file becomes an **integration test** (not unit), because the counter + log assertions are cheap but the `session` fixture requires Postgres. Move the file to `backend/tests/integration/services/nautilus/security_master/test_live_resolver_telemetry.py` and re-use the same per-module `session_factory` + `isolated_postgres_url` fixture pattern defined in Task 3's `test_lookup_for_live.py`. Add `session_with_aapl` as a module-local helper (not global conftest):

```python
# module-local in the telemetry test file
@pytest_asyncio.fixture
async def session_with_aapl(session):
    # session here is the per-module fixture from Task 3's pattern,
    # duplicated into this file's conftest block.
    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition
    d = InstrumentDefinition(
        raw_symbol="AAPL", listing_venue="NASDAQ", routing_venue="SMART",
        asset_class="equity", provider="interactive_brokers",
    )
    session.add(d)
    await session.flush()
    session.add(InstrumentAlias(
        instrument_uid=d.instrument_uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name", provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
    ))
    await session.commit()
    yield session
```

Tests using `caplog` + `counter` only (no DB) stay as unit tests in a sibling file `tests/unit/services/nautilus/security_master/test_live_resolver_counter.py` — the `test_counter_is_registered` test at the bottom of this block doesn't need Postgres and should live there.

**Step 2: Run**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_telemetry.py -v
```

Expected: FAIL — log events + counter not emitted yet.

**Step 3: Implementation**

In `backend/src/msai/services/observability/trading_metrics.py`, append (matching the existing `_r.counter(...)` pattern at lines 18-55):

```python
LIVE_INSTRUMENT_RESOLVED_TOTAL = _r.counter(
    "msai_live_instrument_resolved_total",
    "Count of instrument resolutions on the live-start critical path.",
)
# Labels applied at increment time via .labels(source=..., asset_class=...).inc()
# per the project's hand-rolled Counter API (metrics.py:116-138).
```

In `live_resolver.py`, modify `lookup_for_live` to emit per-symbol log + increment counter on ALL three outcome paths (success / miss / incomplete):

```python
# After each successful resolution:
_log.info(
    "live_instrument_resolved",
    event="live_instrument_resolved",
    source="registry",
    symbol=sym,
    canonical_id=active_alias.alias_string,
    asset_class=ac.value,
    as_of_date=as_of_date.isoformat(),
)
LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
    source="registry", asset_class=ac.value
).inc()

# Before raising RegistryMissError, emit one log per missing symbol:
for m in missing:
    _log.warning(
        "live_instrument_resolved",
        event="live_instrument_resolved",
        source="registry_miss",
        symbol=m,
        as_of_date=as_of_date.isoformat(),
    )
    LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
        source="registry_miss", asset_class="unknown"
    ).inc()

# Inside the _build_contract_spec try/except (Task 8), before re-raising
# RegistryIncompleteError:
except RegistryIncompleteError as inc_exc:
    _log.error(
        "live_instrument_resolved",
        event="live_instrument_resolved",
        source="registry_incomplete",
        symbol=sym,
        missing_field=inc_exc.missing_field,
        as_of_date=as_of_date.isoformat(),
    )
    LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
        source="registry_incomplete", asset_class=ac.value,
    ).inc()
    await _fire_alert_bounded(
        "error",
        "Live instrument registry incomplete",
        str(inc_exc),
    )
    raise
```

Telemetry `source` label has 3 values — `"registry"`, `"registry_miss"`, `"registry_incomplete"`. Counter cardinality ≤ 15 time-series (3 sources × 5 asset classes) — no Prometheus concerns.

Import `LIVE_INSTRUMENT_RESOLVED_TOTAL` at the top of `live_resolver.py`.

**Step 4: Run**

```bash
cd backend && uv run pytest tests/unit/services/nautilus/security_master/test_live_resolver_telemetry.py -v
```

Expected: 3/3 PASS.

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/live_resolver.py \
        backend/src/msai/services/observability/trading_metrics.py \
        backend/tests/unit/services/nautilus/security_master/test_live_resolver_telemetry.py
git commit -m "feat(observability): emit live_instrument_resolved log + counter on resolve

- Per-symbol structured log with source / symbol / canonical_id /
  asset_class / as_of_date fields
- Counter msai_live_instrument_resolved_total via project's hand-rolled
  MetricsRegistry (get_registry().counter(...)) — labels applied at
  increment time; source values: registry, registry_miss
- Miss path emits one log + counter per missing symbol before raising

Refs: PRD §4 US-005 + council verdict constraint #6"
```

---

## Task 8: Alert integration (WARN on miss, ERROR on incomplete)

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/live_resolver.py`
- Test: `backend/tests/integration/services/nautilus/security_master/test_live_resolver_alerts.py` (uses per-module `session_factory` + `isolated_postgres_url` fixture pattern from Task 3's `test_lookup_for_live.py`; no DB-bearing fixtures in root `conftest.py`)

**Step 0: Confirm alerting API (from `backend/src/msai/services/alerting.py:108-128`)**

The project exposes a **sync, file-backed** singleton `alerting_service` (module-level at bottom of file) with signature:

```python
alerting_service.send_alert(level: str, title: str, message: str) -> None
```

- **Sync.** Uses `fcntl.flock` + `os.fsync` internally — can block on contended volumes (existing caller at `alerting.py:313-314` wraps in `loop.run_in_executor` with a 2s timeout for exactly this reason).
- **No `context=` kwarg.** Only `level`, `title`, `message` are accepted.
- `level` strings: `"warning"`, `"error"`, `"info"` (mapped to stdlib log methods via `_LOG_METHOD_BY_ALERT_LEVEL`).
- **MUST wrap in `asyncio.to_thread`** when called from an async function — a bare sync call would block the event loop during a degraded file-lock scenario. This matches `alerting.py:313-314`'s existing production pattern.

**Step 1: Write the failing test**

```python
# backend/tests/integration/services/nautilus/security_master/test_live_resolver_alerts.py
# Reuses the per-module session_factory / isolated_postgres_url / session
# fixture pattern from test_lookup_for_live.py (Task 3). Copy those fixtures
# into this file's module-local conftest block.
from datetime import date
from unittest.mock import MagicMock

import pytest

from msai.services.nautilus.security_master.live_resolver import (
    RegistryMissError,
    lookup_for_live,
)


async def test_registry_miss_fires_warning_alert(monkeypatch, session):
    mock_service = MagicMock()
    monkeypatch.setattr(
        "msai.services.nautilus.security_master.live_resolver.alerting_service",
        mock_service,
    )

    with pytest.raises(RegistryMissError):
        await lookup_for_live(
            ["UNKNOWN"], as_of_date=date(2026, 4, 20), session=session,
        )

    # run_in_executor submits send_alert(level, title, message) with
    # POSITIONAL args — assert accordingly, not via kwargs.
    mock_service.send_alert.assert_called_once()
    args = mock_service.send_alert.call_args.args
    assert args[0] == "warning"                         # level
    assert "registry miss" in args[1].lower()           # title
    assert "UNKNOWN" in args[2]                         # message
    assert "msai instruments refresh" in args[2]
```

**Step 2: Run**

Expected: FAIL — no alert call yet.

**Step 3: Implement**

In `live_resolver.py`, add at the top:

```python
from msai.services.alerting import alerting_service
```

Match the existing production pattern from `alerting.py:305-328` — `run_in_executor` + `asyncio.wait_for(asyncio.shield(...), timeout=_HISTORY_WRITE_TIMEOUT_S)` with log-and-continue on timeout:

```python
import asyncio
import logging

from msai.services.alerting import (
    alerting_service,
    _HISTORY_EXECUTOR,
    _HISTORY_WRITE_TIMEOUT_S,
)

_alert_log = logging.getLogger(__name__)


async def _fire_alert_bounded(
    level: str, title: str, message: str,
) -> None:
    """Bounded file-lock alert write. Matches alerting.py:305-328
    — timeout prevents a wedged alerts volume from hanging the
    live-start critical path.
    """
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(
        _HISTORY_EXECUTOR,
        alerting_service.send_alert,
        level, title, message,
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_HISTORY_WRITE_TIMEOUT_S,
        )
    except TimeoutError:
        _alert_log.warning(
            "alert_history_write_timed_out",
            extra={"title": title, "timeout_s": _HISTORY_WRITE_TIMEOUT_S},
        )
        # Consume the late future — log exceptions so post-timeout
        # failures aren't silently swallowed. Matches alerting.py's
        # _log_history_failure at lines 325-327.
        def _log_late(fut: asyncio.Future, _title: str = title) -> None:
            try:
                fut.result()
            except Exception:  # noqa: BLE001
                _alert_log.exception(
                    "alert_history_late_failed", extra={"title": _title},
                )
            else:
                _alert_log.info(
                    "alert_history_late_complete", extra={"title": _title},
                )
        task.add_done_callback(_log_late)
    except Exception:
        _alert_log.exception("alert_history_write_failed", extra={"title": title})


if missing:
    message = (
        f"Registry miss on symbols {missing!r} as of "
        f"{as_of_date.isoformat()}. Run: msai instruments refresh "
        f"--symbols {','.join(missing)} --provider interactive_brokers"
    )
    await _fire_alert_bounded("warning", "Live instrument registry miss", message)
    raise RegistryMissError(symbols=missing, as_of_date=as_of_date)
```

For incomplete errors, wrap `_build_contract_spec` calls in try/except. On `RegistryIncompleteError`, call `await _fire_alert_bounded("error", ...)` with the specific `symbol` + `missing_field` in the message, then re-raise.

Note: `_HISTORY_EXECUTOR` and `_HISTORY_WRITE_TIMEOUT_S` are module-private in `alerting.py` — the import uses leading-underscore private names. Acceptable because `_fire_alert_bounded` lives in the same project's resolver module. If cross-module access becomes a concern, expose a public `alerting.send_alert_bounded(level, title, message)` async helper that does the same dance — one-line plan addition.

Update Task 8's test: the mock replaces `alerting_service` (sync singleton); the assertion pattern is `mock_service.send_alert.assert_called_once_with(level="warning", title="...", message=...)` after `await _fire_alert_bounded(...)` completes. Use `MagicMock` (NOT `AsyncMock`) — the mock replaces the sync method that `run_in_executor` wraps. Add a timeout regression test: patch `_HISTORY_EXECUTOR` to submit a future that never completes within `_HISTORY_WRITE_TIMEOUT_S`; assert `_alert_log` records `"alert_history_write_timed_out"` and `lookup_for_live` still raises `RegistryMissError` (alerting failure doesn't poison the resolver's error classification).

**Step 4: Run**

Expected: PASS.

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/security_master/live_resolver.py \
        backend/tests/unit/services/nautilus/security_master/test_live_resolver_alerts.py
git commit -m "feat(security_master): fire WARN alert on registry miss, ERROR on incomplete

Matches PRD §4 US-002 / US-006. Miss = operator-recoverable; incomplete
= data-integrity issue."
```

---

## Task 9: Supervisor wiring — replace canonical_instrument_id with lookup_for_live

**Files:**

- Modify: `backend/src/msai/services/live/failure_kind.py` (add 3 new enum variants)
- Modify: `backend/src/msai/live_supervisor/__main__.py:~281-285` (resolver call in payload factory)
- Modify: `backend/src/msai/live_supervisor/process_manager.py:253-297` (dispatch on `LiveResolverError` subtype in the permanent-catch)
- Test: `backend/tests/integration/live_supervisor/test_supervisor_uses_lookup_for_live.py`
- Test: `backend/tests/unit/live_supervisor/test_process_manager_registry_dispatch.py`

**Context: the real contract (`process_manager.py:253-297`)**

The payload factory is called inside `ProcessManager.spawn()`. Raising `ValueError` (or any of `ImportError`/`ModuleNotFoundError`/`FileNotFoundError`/`AttributeError`) lands in the permanent-catch, which calls `_mark_failed(row_id, reason, failure_kind=SPAWN_FAILED_PERMANENT)` and ACKs the command. The API handler `/start-portfolio` then polls `live_node_processes` and reads `failure_kind` + `error_message` to shape the HTTP response (`api/live.py:627-654`).

**There is no `command_bus.publish_failure`** — `LiveCommandBus` only has `publish_start`/`publish_stop`. **There is no `deployment.status = "failed"`** path in the payload factory — failure state lives on `live_node_processes.failure_kind`, not `live_deployments.status`.

**Step 1: Add FailureKind variants**

In `backend/src/msai/services/live/failure_kind.py`, add three new enum values near `SPAWN_FAILED_PERMANENT` (they are permanent from the endpoint's perspective, but classified distinctly so HTTP responses can return specific error codes):

```python
REGISTRY_MISS = "registry_miss"
"""``lookup_for_live`` raised ``RegistryMissError`` — one or more
symbols lack an active registry alias. Permanent; operator must run
``msai instruments refresh --symbols <X>`` before retrying. Endpoint
maps this to HTTP 422 code=REGISTRY_MISS."""

REGISTRY_INCOMPLETE = "registry_incomplete"
"""``lookup_for_live`` raised ``RegistryIncompleteError`` — a matched
row has NULL/malformed required fields. Data-integrity issue; fire
ERROR alert. Endpoint maps to HTTP 422 code=REGISTRY_INCOMPLETE."""

UNSUPPORTED_ASSET_CLASS = "unsupported_asset_class"
"""``lookup_for_live`` raised ``UnsupportedAssetClassError`` — a row
resolved to ``option`` or ``crypto``. Endpoint maps to HTTP 422
code=UNSUPPORTED_ASSET_CLASS."""

AMBIGUOUS_REGISTRY = "ambiguous_registry"
"""``lookup_for_live`` raised ``AmbiguousRegistryError`` — a bare
symbol matches multiple registry rows across asset_classes, OR
multiple active aliases share the same ``effective_from`` (operator-
seeded overlap). Endpoint maps to HTTP 422 code=AMBIGUOUS_REGISTRY."""
```

Update the endpoint's `permanent_kinds` set in `api/live.py:642-648` to include the three new variants (otherwise they'd collapse to `UNKNOWN`).

**Step 2: Dispatch in `ProcessManager.spawn()` permanent-catch**

At `backend/src/msai/live_supervisor/process_manager.py:261-297`, extend the `except` block to dispatch on `LiveResolverError` subtype BEFORE the generic `ValueError` branch:

```python
except (
    ValueError,
    ImportError,
    ModuleNotFoundError,
    FileNotFoundError,
    AttributeError,
) as exc:
    # Dispatch on resolver-specific subtypes so the endpoint can
    # return distinct HTTP error codes (REGISTRY_MISS vs
    # REGISTRY_INCOMPLETE vs UNSUPPORTED_ASSET_CLASS vs
    # AMBIGUOUS_REGISTRY vs the generic SPAWN_FAILED_PERMANENT
    # fallback).
    from msai.services.nautilus.security_master.live_resolver import (
        AmbiguousRegistryError,
        LiveResolverError,
        RegistryIncompleteError,
        RegistryMissError,
        UnsupportedAssetClassError,
    )
    if isinstance(exc, RegistryMissError):
        kind = FailureKind.REGISTRY_MISS
    elif isinstance(exc, RegistryIncompleteError):
        kind = FailureKind.REGISTRY_INCOMPLETE
    elif isinstance(exc, UnsupportedAssetClassError):
        kind = FailureKind.UNSUPPORTED_ASSET_CLASS
    elif isinstance(exc, AmbiguousRegistryError):
        kind = FailureKind.AMBIGUOUS_REGISTRY
    else:
        kind = FailureKind.SPAWN_FAILED_PERMANENT

    # For resolver-class errors, persist the structured JSON envelope
    # as reason so the EndpointOutcome factory can parse back into
    # {code, message, details}. For other errors, preserve the
    # existing "payload factory failed (permanent): " prefix.
    if isinstance(exc, LiveResolverError):
        reason = exc.to_error_message()
    else:
        reason = f"payload factory failed (permanent): {exc}"

    log.exception("spawn_payload_factory_failed_permanent", extra={
        "deployment_id": str(deployment_id),
        "deployment_slug": deployment_slug,
        "exception_type": type(exc).__name__,
        "failure_kind": kind.value,
    })
    await self._mark_failed(
        row_id=row_id,
        reason=reason,
        failure_kind=kind,
    )
    return True
```

**Step 3: Supervisor payload-factory change (`__main__.py:~281-285`)**

Replace:

```python
# BEFORE
member_canonical = [
    canonical_instrument_id(inst, today=spawn_today)
    for inst in member.instruments
]
```

with:

```python
# AFTER
from msai.services.nautilus.security_master.live_resolver import (
    lookup_for_live,
)

# Defensive guard — an empty member.instruments is a programmer bug
# (portfolio revision freeze should have rejected it). Raise a clear
# ValueError here so the permanent-catch gets a descriptive reason
# instead of the resolver's generic "symbols cannot be empty". NOTE:
# strategy_id_full is a LOCAL variable already computed at line 274
# of __main__.py (via derive_strategy_id_full) — it is NOT an
# attribute on the LivePortfolioRevisionStrategy ORM row.
if not member.instruments:
    raise ValueError(
        f"strategy member {strategy_id_full!r} has no instruments "
        "— portfolio freeze should have rejected this revision"
    )

# member.instruments contract: list[str] (ARRAY(String) per
# live_portfolio_revision_strategy.py:59). Shape is free-form —
# callers typically store dotted aliases (AAPL.NASDAQ) but bare
# tickers also work. The resolver's "." in sym branch dispatches
# correctly for either shape.

# Pure-read resolver — raises RegistryMissError /
# RegistryIncompleteError / UnsupportedAssetClassError /
# AmbiguousRegistryError (all subclass LiveResolverError → ValueError,
# so ProcessManager's permanent-catch fires and dispatches on subtype
# to the distinct FailureKind).
resolved_instruments = await lookup_for_live(
    list(member.instruments),
    as_of_date=spawn_today,
    session=session,
)
member_canonical = [r.canonical_id for r in resolved_instruments]
member_resolved = tuple(resolved_instruments)  # tuple for pickle-friendliness
```

No try/except here — resolver errors MUST propagate so `ProcessManager` classifies them. Wrapping would swallow the classification signal.

**CRITICAL — Task 9 Step 4 follow-through:** The `StrategyMemberPayload(...)` construction at `__main__.py:318-327` currently reads:

```python
strategy_members.append(
    StrategyMemberPayload(
        strategy_id=strat.id,
        strategy_path=paths.strategy_path,
        strategy_config_path=paths.config_path,
        strategy_config=member_config,
        strategy_code_hash=member_code_hash,
        strategy_id_full=strategy_id_full,
        instruments=member_paper_symbols,
    )
)
```

Must be updated to thread the resolved tuple through:

```python
strategy_members.append(
    StrategyMemberPayload(
        strategy_id=strat.id,
        strategy_path=paths.strategy_path,
        strategy_config_path=paths.config_path,
        strategy_config=member_config,
        strategy_code_hash=member_code_hash,
        strategy_id_full=strategy_id_full,
        instruments=member_paper_symbols,
        resolved_instruments=member_resolved,  # ← NEW
    )
)
```

Without this, the new field defaults to `()` and Task 11's `if not aggregated: raise` would fire on every deploy. Integration test MUST assert `payload.strategy_members[i].resolved_instruments` is non-empty.

**Step 4: Thread `member_resolved` onto the subprocess payload**

`TradingNodePayload` / `StrategyMemberPayload` must carry the tuple so the subprocess's `live_node_config.py:478` can consume them (Task 11). Add a `resolved_instruments: tuple[ResolvedInstrument, ...] = ()` field to `StrategyMemberPayload` (frozen dataclass at `services/nautilus/trading_node_subprocess.py:~100`). The pickle-safety test for this lives in Task 11b (new).

**Step 5: Write failing integration test**

```python
# backend/tests/integration/live_supervisor/test_supervisor_uses_lookup_for_live.py
"""Supervisor payload factory must call lookup_for_live and raise on miss.

Seeds the registry with QQQ; spawns a deployment via the payload factory;
asserts ResolvedInstrument tuple reaches the StrategyMemberPayload. Then
seeds a portfolio member with an un-warmed symbol and asserts
ProcessManager._mark_failed is called with FailureKind.REGISTRY_MISS.
"""
# Full body uses the per-module session_factory pattern (same as Task 3).
```

**Step 6: Write failing unit test for ProcessManager dispatch**

```python
# backend/tests/unit/live_supervisor/test_process_manager_registry_dispatch.py
import pytest

from msai.services.live.failure_kind import FailureKind
from msai.services.nautilus.security_master.live_resolver import (
    RegistryMissError,
    RegistryIncompleteError,
    UnsupportedAssetClassError,
    AssetClass,
)


async def test_registry_miss_maps_to_registry_miss_kind(
    process_manager_with_mocked_mark_failed,
):
    factory = _raising_payload_factory(
        RegistryMissError(symbols=["UNKNOWN"], as_of_date=_today())
    )
    pm = process_manager_with_mocked_mark_failed(factory)
    await pm._spawn_one(...)  # invoke the relevant private path
    kwargs = pm._mark_failed.call_args.kwargs
    assert kwargs["failure_kind"] is FailureKind.REGISTRY_MISS


async def test_registry_incomplete_maps_to_registry_incomplete_kind(...): ...
async def test_unsupported_asset_class_maps_to_unsupported_kind(...): ...
async def test_generic_value_error_still_maps_to_spawn_failed_permanent(...): ...
```

The test fixture mocks `_mark_failed` so we can assert the `failure_kind` arg without touching Postgres.

**Step 7: Run all → PASS**

```bash
cd backend && uv run pytest tests/integration/live_supervisor/ tests/unit/live_supervisor/ -v
```

**Step 8: Commit**

```bash
git add backend/src/msai/services/live/failure_kind.py \
        backend/src/msai/live_supervisor/__main__.py \
        backend/src/msai/live_supervisor/process_manager.py \
        backend/src/msai/services/nautilus/trading_node_subprocess.py \
        backend/src/msai/api/live.py \
        backend/tests/integration/live_supervisor/test_supervisor_uses_lookup_for_live.py \
        backend/tests/unit/live_supervisor/test_process_manager_registry_dispatch.py
git commit -m "feat(live_supervisor): wire lookup_for_live + classify resolver errors

- Add FailureKind.{REGISTRY_MISS,REGISTRY_INCOMPLETE,UNSUPPORTED_ASSET_CLASS}
- Payload factory calls lookup_for_live; errors propagate (no catch)
- ProcessManager._mark_failed dispatches on subtype before calling
  _mark_failed with distinct FailureKind
- StrategyMemberPayload carries tuple[ResolvedInstrument,...] through
  to the subprocess (pickle-safe; test in Task 11b)
- Endpoint permanent_kinds set extended to include the new variants

Refs: docs/decisions/live-path-registry-wiring.md blocking constraint #1
+ council non-goal (no HTTP preflight); error classification via
existing failure_kind flow"
```

---

## Task 10: IB preload builder accepts ResolvedInstrument — remove PHASE_1_PAPER_SYMBOLS gate

**Files:**

- Modify: `backend/src/msai/services/nautilus/live_instrument_bootstrap.py:~270-315`
- Test: `backend/tests/unit/services/nautilus/test_live_instrument_bootstrap.py` (add)

**Step 1: Failing test**

```python
def test_build_ib_instrument_provider_config_accepts_resolved_instruments():
    from msai.services.nautilus.security_master.live_resolver import (
        AssetClass, ResolvedInstrument,
    )
    from msai.services.nautilus.live_instrument_bootstrap import (
        build_ib_instrument_provider_config_from_resolved,
    )
    from datetime import date

    resolved = [
        ResolvedInstrument(
            canonical_id="QQQ.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK", "symbol": "QQQ",
                "exchange": "SMART", "primaryExchange": "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    ]
    cfg = build_ib_instrument_provider_config_from_resolved(resolved)
    assert len(cfg.load_contracts) == 1
    contract = next(iter(cfg.load_contracts))
    assert contract.secType == "STK"
    assert contract.symbol == "QQQ"
    assert contract.primaryExchange == "NASDAQ"
```

**Step 2: Run — FAIL (function missing).**

**Step 3: Implement**

In `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`, add:

```python
def build_ib_instrument_provider_config_from_resolved(
    resolved: list["ResolvedInstrument"],  # type: ignore[name-defined]
) -> InteractiveBrokersInstrumentProviderConfig:
    """Registry-backed counterpart to build_ib_instrument_provider_config.

    Takes ResolvedInstrument objects (from lookup_for_live) and
    reconstructs IBContract from each contract_spec dict.

    No PHASE_1_PAPER_SYMBOLS gate — any well-formed contract_spec is
    accepted. Fail-fast validation is lookup_for_live's job.
    """
    contracts = frozenset(_ibcontract_from_spec(r.contract_spec) for r in resolved)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )


def _ibcontract_from_spec(spec: dict[str, object]) -> IBContract:
    """Reconstruct an IBContract from a resolver-produced contract_spec."""
    # IBContract accepts kwargs directly — filter to known fields
    valid = {
        "secType", "symbol", "exchange", "primaryExchange",
        "currency", "lastTradeDateOrContractMonth",
    }
    return IBContract(**{k: v for k, v in spec.items() if k in valid})
```

Keep `build_ib_instrument_provider_config` (the PHASE_1-gated variant) as a compatibility shim for `cli.py` and `instruments.py` — but delete its call site inside `live_node_config.py:478` in Task 11.

**Step 4: Run — PASS.**

**Step 5: Commit**

```bash
git add backend/src/msai/services/nautilus/live_instrument_bootstrap.py \
        backend/tests/unit/services/nautilus/test_live_instrument_bootstrap.py
git commit -m "feat(nautilus): build_ib_instrument_provider_config_from_resolved()

- Accepts list[ResolvedInstrument] from lookup_for_live; constructs
  IBContract from each contract_spec dict
- No PHASE_1_PAPER_SYMBOLS gate on this path
- Legacy build_ib_instrument_provider_config retained for CLI/seeding

Refs: council verdict blocking constraint #1 (IB preload must be
wired or registry stays half-plugged)"
```

---

## Task 11: live_node_config.py wiring + remove PHASE_1 gate from runtime

**Files:**

- Modify: `backend/src/msai/services/nautilus/live_node_config.py:~478-481`
- Modify: `backend/src/msai/services/nautilus/trading_node_subprocess.py` (thread ResolvedInstrument through payload)

**Correct target:** the real function is `build_portfolio_trading_node_config()` at `live_node_config.py:416` (NOT `build_live_node_config` — that name does not exist). It currently aggregates `member.instruments` across `strategy_members` into `sorted_instruments: list[str]` (line 465-473) and passes them to `build_ib_instrument_provider_config(sorted_instruments, today=spawn_today)` at line 478.

**After this task:**

- Each `StrategyMemberPayload` already carries `resolved_instruments: tuple[ResolvedInstrument, ...]` (from Task 9 Step 4).
- `build_portfolio_trading_node_config` aggregates `member.resolved_instruments` across members (dedup by `canonical_id`) and passes the flattened list to `build_ib_instrument_provider_config_from_resolved()`.
- The old `sorted_instruments` string list is no longer consumed by the provider config. It may still be useful for audit logs.

**Step 1: Failing test**

`backend/tests/unit/services/nautilus/test_live_node_config_registry.py` — construct a `strategy_members: list[StrategyMemberPayload]` with two members each carrying different `resolved_instruments`; call `build_portfolio_trading_node_config` and assert the returned `TradingNodeConfig` has `load_contracts` containing the `IBContract`s reconstructed from all members' `resolved_instruments`, deduplicated on `canonical_id` across members.

**Step 2: Run — FAIL.**

**Step 3: Implement**

At `live_node_config.py:465-481`, replace the `sorted_instruments` aggregation + `build_ib_instrument_provider_config(sorted_instruments, today=spawn_today)` call with:

```python
# Aggregate resolved instruments across all strategy_members.
# Dedup on canonical_id so two strategies subscribing to the same
# instrument produce one IBContract, not two.
from msai.services.nautilus.live_instrument_bootstrap import (
    build_ib_instrument_provider_config_from_resolved,
)

seen: dict[str, ResolvedInstrument] = {}
for member in strategy_members:
    for ri in member.resolved_instruments:
        seen.setdefault(ri.canonical_id, ri)
aggregated: list[ResolvedInstrument] = list(seen.values())

if not aggregated:
    raise ValueError(
        "No resolved instruments found across strategy_members — a "
        "TradingNode with no subscribed instruments cannot make progress."
    )

instrument_provider_config = build_ib_instrument_provider_config_from_resolved(
    aggregated,
)
```

Update `build_portfolio_trading_node_config`'s internal logic only. The signature (`*, deployment_slug, strategy_members, ib_settings, ..., spawn_today=None`) stays the same — the input-shape change is on `StrategyMemberPayload`, not on this function's kwargs.

**Step 4: Run — PASS.**

**Step 5: Commit**

```bash
git commit -m "feat(nautilus): build_portfolio_trading_node_config aggregates resolved_instruments

- Consumes member.resolved_instruments (added in Task 9) instead of
  raw string symbols + PHASE_1_PAPER_SYMBOLS gate
- Dedups by canonical_id across members
- Signature unchanged — input shape change lives on StrategyMemberPayload

Refs: council verdict constraint #1 (three surface wiring complete)"
```

---

## Task 11b: Pickle round-trip test for TradingNodePayload with ResolvedInstrument

**Files:**

- Modify: `backend/tests/unit/test_trading_node_payload_multi_strategy.py` (append)

**Context:** `TradingNodePayload` and `StrategyMemberPayload` are `@dataclass(frozen=True)` with a documented invariant: "only-primitive fields so `mp.Process` can pickle it under the spawn context" (`trading_node_subprocess.py:118-126`). Adding `resolved_instruments: tuple[ResolvedInstrument, ...]` extends the payload; we must prove pickle-safety BEFORE the subprocess consumes it (Task 11).

**Step 1: Write the failing / locking test**

Use the existing `_make_member()` helper at `test_trading_node_payload_multi_strategy.py:24-41` (takes keyword-only args with sensible defaults, so passing `resolved_instruments=...` as a new kwarg requires only the one change). Pattern:

```python
# append to backend/tests/unit/test_trading_node_payload_multi_strategy.py
import pickle
from datetime import date

from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
)
# _make_member + any existing helpers are already in-module.


def test_payload_pickles_with_resolved_instruments():
    """Lock the pickle round-trip invariant — prevents a future field
    addition (Decimal, datetime, Path) from silently breaking mp.spawn.
    """
    resolved = (
        ResolvedInstrument(
            canonical_id="QQQ.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK", "symbol": "QQQ",
                "exchange": "SMART", "primaryExchange": "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    )
    # _make_member already fills in strategy_id, strategy_path,
    # strategy_config_path, strategy_config, strategy_code_hash,
    # strategy_id_full — we only override resolved_instruments.
    member = _make_member(resolved_instruments=resolved)

    # Re-use the full-payload construction pattern from the existing
    # "TradingNodePayload fields" test at lines ~100-120 of this file.
    # (Plan omits the full TradingNodePayload kwargs here; the
    # executor copies them from the nearest existing test.)
    payload = _make_payload(strategy_members=[member])  # helper to be added if not present

    round_tripped = pickle.loads(pickle.dumps(payload))
    assert round_tripped.strategy_members[0].resolved_instruments == resolved
    assert round_tripped.strategy_members[0].resolved_instruments[0].asset_class is AssetClass.EQUITY
```

Also extend `_make_member()` at line 24-41 to accept `resolved_instruments: tuple[ResolvedInstrument, ...] = ()` as a new kwarg-only parameter with empty tuple default (matches the `StrategyMemberPayload` field default added in Task 9 Step 4). That keeps all 8+ existing call sites green without modification.

**Step 2: Run — FAIL**

```bash
cd backend && uv run pytest tests/unit/test_trading_node_payload_multi_strategy.py::test_payload_pickles_with_resolved_instruments_via_spawn_context -v
```

Expected: FAIL until `StrategyMemberPayload.resolved_instruments` field is added (happens in Task 9 Step 4).

**Step 3: Commit (together with Task 9 or separately if that task is already merged)**

```bash
git commit -m "test(subprocess): lock pickle round-trip of TradingNodePayload with ResolvedInstrument

Prevents a future field addition (Decimal, datetime, etc.) from silently
breaking mp.spawn. Pairs with Task 9's StrategyMemberPayload extension."
```

---

## Task 12: New `registry_permanent_failure` factory + endpoint dispatch

**Files (real paths — verified in iter-2):**

- Modify: `backend/src/msai/services/live/idempotency.py:201-240` (existing `EndpointOutcome` class + `_PERMANENT_FAILURE_KINDS` set live here — NOT in `services/live/endpoint_outcome.py` which does not exist)
- Modify: `backend/src/msai/api/live.py:626-654` (handler dispatches on kind: registry vs. legacy)
- Test: `backend/tests/integration/api/test_live_start_portfolio_registry_errors.py`
- Test: `backend/tests/unit/services/live/test_endpoint_outcome_registry_factory.py`

**Context (real contract):**

- `EndpointOutcome.permanent_failure(row_failure_kind, error_message)` at `idempotency.py:201-229` returns `status_code=503, response={"detail": error_message, "failure_kind": row_failure_kind.value}, cacheable=True`.
- It `assert`s `row_failure_kind in _PERMANENT_FAILURE_KINDS` (`idempotency.py:232-240`).
- Handler at `api/live.py:642-651` calls `EndpointOutcome.permanent_failure(kind, row.error_message or "unknown failure")` for all permanent kinds.

We need distinct response contracts for the 3 new registry kinds (HTTP 422 + `{"error": {...}}` envelope per `.claude/rules/api-design.md`) while leaving the legacy 503/`{"detail":...}` contract untouched for existing kinds. Add a **new** factory, not an extension of the old one.

**Step 1: Failing unit test for the new factory**

```python
# backend/tests/unit/services/live/test_endpoint_outcome_registry_factory.py
import json

from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import EndpointOutcome


def test_registry_permanent_failure_parses_json_error_message_into_details():
    error_message = json.dumps({
        "code": "REGISTRY_MISS",
        "message": "Symbol(s) not in registry: ['QQQ'] as of 2026-04-20. Run: msai instruments refresh --symbols QQQ --provider interactive_brokers",
        "details": {"missing_symbols": ["QQQ"], "as_of_date": "2026-04-20"},
    })

    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS, error_message,
    )

    assert outcome.status_code == 422
    assert outcome.cacheable is False  # operator-correctable, not cached
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert "msai instruments refresh" in body["error"]["message"]
    assert body["error"]["details"]["missing_symbols"] == ["QQQ"]
    assert body["failure_kind"] == "registry_miss"


def test_registry_permanent_failure_rejects_non_registry_kind():
    import pytest
    with pytest.raises(AssertionError):
        EndpointOutcome.registry_permanent_failure(
            FailureKind.SPAWN_FAILED_PERMANENT, "{}",
        )


def test_registry_permanent_failure_falls_back_on_non_json_message():
    """Defensive: if error_message isn't valid JSON (legacy row,
    corrupt write), the factory still returns a well-formed body
    with the raw string as message and an empty details dict."""
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS, "plain text from older version",
    )
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert body["error"]["message"] == "plain text from older version"
    assert body["error"]["details"] == {}
```

**Step 2: Run — FAIL** (factory doesn't exist yet).

**Step 3: Implement factory in `idempotency.py`**

Add the new `_REGISTRY_FAILURE_KINDS` set + the `registry_permanent_failure` factory. Append after `_PERMANENT_FAILURE_KINDS` at line 240:

```python
_REGISTRY_FAILURE_KINDS: frozenset[FailureKind] = frozenset(
    {
        FailureKind.REGISTRY_MISS,
        FailureKind.REGISTRY_INCOMPLETE,
        FailureKind.UNSUPPORTED_ASSET_CLASS,
        FailureKind.AMBIGUOUS_REGISTRY,
    }
)
```

Add to `EndpointOutcome` class (after the existing `permanent_failure` factory):

```python
@classmethod
def registry_permanent_failure(
    cls,
    row_failure_kind: FailureKind,
    error_message: str,
) -> EndpointOutcome:
    """Build a cacheable HTTP 422 from a DB row for the three
    registry-class ``failure_kind`` values. Accepts
    ``REGISTRY_MISS``, ``REGISTRY_INCOMPLETE``, and
    ``UNSUPPORTED_ASSET_CLASS``.

    ``error_message`` is expected to be a JSON-encoded envelope
    produced by ``LiveResolverError.to_error_message()``:
    ``{"code": "...", "message": "...", "details": {...}}``.
    On parse failure (legacy row, hand-edited column), the whole
    string becomes the ``message`` and ``details`` is ``{}``.

    Response envelope follows ``.claude/rules/api-design.md``:
    ``{"error": {"code": str, "message": str, "details": dict},
       "failure_kind": str}``.
    """
    import json as _json

    assert row_failure_kind in _REGISTRY_FAILURE_KINDS, (
        f"registry_permanent_failure called with non-registry kind: {row_failure_kind!r}"
    )

    try:
        parsed = _json.loads(error_message)
    except (_json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    code = parsed.get("code") or row_failure_kind.value.upper()
    message = parsed.get("message") or error_message
    details = parsed.get("details") if isinstance(parsed.get("details"), dict) else {}

    return cls(
        status_code=422,
        response={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            },
            "failure_kind": row_failure_kind.value,
        },
        # Registry failures are OPERATOR-CORRECTABLE (run `msai
        # instruments refresh` / close alias / pick supported asset
        # class) — caching the 422 would block retry-after-fix with
        # the same Idempotency-Key. Unlike permanent_failure (503 /
        # build-timeout / reconciliation), retrying after operator
        # correction is the expected recovery path.
        cacheable=False,
        failure_kind=row_failure_kind,
    )
```

**Step 4: Wire the handler at `api/live.py:642-651`**

```python
permanent_kinds = {
    FailureKind.SPAWN_FAILED_PERMANENT,
    FailureKind.RECONCILIATION_FAILED,
    FailureKind.BUILD_TIMEOUT,
    FailureKind.HEARTBEAT_TIMEOUT,
    FailureKind.REGISTRY_MISS,
    FailureKind.REGISTRY_INCOMPLETE,
    FailureKind.UNSUPPORTED_ASSET_CLASS,
    FailureKind.AMBIGUOUS_REGISTRY,
    FailureKind.UNKNOWN,
}
if kind not in permanent_kinds:
    kind = FailureKind.UNKNOWN

from msai.services.live.idempotency import _REGISTRY_FAILURE_KINDS

if kind in _REGISTRY_FAILURE_KINDS:
    outcome = EndpointOutcome.registry_permanent_failure(
        kind, row.error_message or "{}",
    )
else:
    outcome = EndpointOutcome.permanent_failure(
        kind, row.error_message or "unknown failure",
    )
if reservation is not None:
    await idem.commit(reservation.redis_key, body_hash, outcome)
return _apply_outcome(outcome)
```

**Step 5: Failing integration test**

```python
# backend/tests/integration/api/test_live_start_portfolio_registry_errors.py
"""End-to-end HTTP test using the real PortfolioStartRequest schema
(schemas/live.py:21-32) and the client pattern from
test_live_start_endpoints.py:160-206.
"""

async def test_start_portfolio_registry_miss_returns_422(
    client, session_factory, seed_portfolio_with_unknown_symbol, test_user
):
    response = await client.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(seed_portfolio_with_unknown_symbol),
            "account_id": "DU1234567",
            "paper_trading": True,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert "msai instruments refresh" in body["error"]["message"]
    assert body["error"]["details"]["missing_symbols"]
    assert body["failure_kind"] == "registry_miss"


async def test_start_portfolio_registry_incomplete_returns_422_with_symbol_field(client, ...): ...


async def test_start_portfolio_option_returns_422_unsupported(client, ...): ...
```

The `seed_portfolio_with_unknown_symbol` fixture creates a `LivePortfolio` + `LivePortfolioRevision` (frozen) whose members reference a symbol NOT in the registry. Use `make_live_deployment` from `tests/integration/_deployment_factory.py` for the deployment row portion.

**Step 6: Run all → PASS**

```bash
cd backend && uv run pytest tests/unit/services/live/test_endpoint_outcome_registry_factory.py tests/integration/api/test_live_start_portfolio_registry_errors.py -v
```

**Step 7: Commit**

```bash
git add backend/src/msai/services/live/idempotency.py \
        backend/src/msai/api/live.py \
        backend/tests/unit/services/live/test_endpoint_outcome_registry_factory.py \
        backend/tests/integration/api/test_live_start_portfolio_registry_errors.py
git commit -m "feat(api): EndpointOutcome.registry_permanent_failure returns HTTP 422 + {error:{...}}

- New factory alongside the existing permanent_failure (503 / detail)
- Parses JSON-encoded error_message from LiveResolverError.to_error_message()
  into {code, message, details}; defensive fallback for non-JSON values
- _PERMANENT_FAILURE_KINDS extended to include the 3 registry kinds
- Handler dispatches: _REGISTRY_FAILURE_KINDS → new 422 factory;
  legacy kinds stay on 503 factory (no breaking change for existing
  cached responses)
- Response envelope matches .claude/rules/api-design.md

Refs: PRD §4 US-002 acceptance criterion (details.missing_symbols);
council non-goal for HTTP preflight — error surface via existing
poll-and-classify flow"
```

---

## Task 13: AST-based regression — no canonical_instrument_id in runtime paths

**Files:**

- Modify: `backend/src/msai/live_supervisor/__main__.py` (remove line ~283 call + the `from ...live_instrument_bootstrap import canonical_instrument_id` at ~line 53)
- Modify: `backend/src/msai/services/nautilus/live_node_config.py` (remove line ~478 legacy call + any remaining imports)
- Test: `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py`

**Why AST, not grep:** a grep for `canonical_instrument_id(` misses aliased imports (`from X import canonical_instrument_id as _ci; ...; _ci(x)`), reflective access (`getattr(mod, "canonical_instrument_id")(x)`), and re-exports. An AST walk identifies every `Import`/`ImportFrom` + every `Name`/`Attribute` reference.

**Step 1: Write the failing test**

```python
# backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py
"""Structural regression — council verdict constraint #4.

canonical_instrument_id() must stay for CLI seeding in
live_instrument_bootstrap.py (definition site) and the cold-path
resolver in service.py, but must not appear in the live-start
runtime paths.
"""
from __future__ import annotations

import ast
import pathlib

FORBIDDEN_NAME = "canonical_instrument_id"

RUNTIME_FILES = (
    "backend/src/msai/live_supervisor/__main__.py",
    "backend/src/msai/services/nautilus/live_node_config.py",
    # live_instrument_bootstrap.py is EXCLUDED — it's the definition
    # site, and the post-wiring plan keeps the function for CLI
    # seeding only (not called internally from the runtime helper
    # build_ib_instrument_provider_config_from_resolved).
)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).parents[4]


def _ast_references(path: pathlib.Path, name: str) -> list[tuple[int, str]]:
    """Return every location where ``name`` appears as an identifier.

    Matches: bare name references (``canonical_instrument_id``),
    attribute access (``mod.canonical_instrument_id``), import-from
    (``from x import canonical_instrument_id`` or ``... as _y``),
    and plain imports. Does NOT match string literals / docstrings.
    """
    tree = ast.parse(path.read_text())
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            hits.append((node.lineno, "name_ref"))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            hits.append((node.lineno, "attr_access"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == name:
                    hits.append((node.lineno, f"import_from:{alias.asname or alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(f".{name}") or alias.name == name:
                    hits.append((node.lineno, f"import:{alias.asname or alias.name}"))
    return hits


def test_canonical_instrument_id_absent_from_runtime_paths():
    root = _repo_root()
    violations: dict[str, list[tuple[int, str]]] = {}
    for rel in RUNTIME_FILES:
        p = root / rel
        if not p.exists():
            continue
        hits = _ast_references(p, FORBIDDEN_NAME)
        if hits:
            violations[rel] = hits
    assert not violations, (
        f"canonical_instrument_id still referenced in runtime files: "
        f"{violations!r}. Council constraint #4: helper leaves the "
        "runtime path (stays in CLI + bootstrap seeding only)."
    )
```

**Step 2: Run — FAIL (imports + calls still present in supervisor + live_node_config).**

**Step 3: Remove imports + calls.** (Most removal is staged in Tasks 9 + 11; this task removes the residual `from msai.services.nautilus.live_instrument_bootstrap import canonical_instrument_id` line at `__main__.py:~53-56` and any unused imports in `live_node_config.py`.)

**Step 4: Run — PASS.**

**Step 5: Commit**

```bash
git commit -m "test(structure): lock 'canonical_instrument_id leaves runtime' invariant

Per council verdict: helper stays for CLI/bootstrap seeding, but
must not appear in supervisor or live_node_config or IB preload
builder runtime paths."
```

---

## Task 14: E2E use cases designed (Phase 3.2b)

**Files:**

- Create: `backend/tests/e2e/use-cases/live/registry-backed-deploy.md` (drafted here; graduates in Phase 6.2b)

Write use-case markdown covering:

- **UC-L-REG-001**: Deploy QQQ via paper IB after `msai instruments refresh` — Intent / Steps / Verification / Persistence.
- **UC-L-REG-002**: Deploy un-warmed GBP/USD → HTTP 422 REGISTRY_MISS with copy-pastable command.
- **UC-L-REG-003**: Futures-roll day — deploy ES on 2026-06-19 vs 2026-06-20; verify different contract months subscribed.
- **UC-L-REG-004**: Option in portfolio rejected with HTTP 422 UNSUPPORTED_ASSET_CLASS.
- **UC-L-REG-005**: Telemetry check — confirm `live_instrument_resolved` log + Prometheus counter increment during UC-L-REG-001.

**Commit:**

```bash
git commit -m "docs(e2e): draft use cases for registry-backed live-start"
```

---

## Task 15: Drill procedure + real-money drill on U4705114 (blocking gate)

**Files:**

- Create: `docs/runbooks/drill-live-path-registry-wiring.md`
- Create: `docs/runbooks/drill-reports/2026-04-??-live-path-registry-drill.md` (post-drill)

**Procedure:**

1. **Pre-flight A — Registry seed:** `msai instruments refresh --symbols QQQ --provider interactive_brokers`; verify row inserted via `msai data-status` equivalent.
   1a. **Pre-flight B — Databento alias co-existence check.** Before running the drill, sanity-check the registry for any `provider='databento'` rows on symbols the drill touches — those use a different alias format (`.F.0` pattern) and would fail `_parse_futures_expiry` silently if a future code path cross-contaminates providers. Run:
   `bash
psql $DATABASE_URL -c "SELECT DISTINCT provider, COUNT(*) FROM instrument_aliases GROUP BY provider"
`
   Expected: `interactive_brokers` rows present; `databento` rows either absent or only on futures-root symbols not touched by this drill. If Databento rows exist on drill symbols, log the finding in the drill report as context (not a blocker — resolver's `provider="interactive_brokers"` filter prevents cross-contamination).
2. Switch IB Gateway to live (`IB_GATEWAY_PORT=4001`, account `U4705114`).
3. Create portfolio revision containing strategy X with instrument QQQ; freeze.
4. `POST /api/v1/live/start-portfolio`; tail supervisor + subprocess logs.
5. Confirm `live_instrument_resolved{source="registry",symbol="QQQ"}` log appears.
6. Wait for first bar event (< 60s).
7. Deploy strategy sends BUY 1 share.
8. Verify fill in `/api/v1/live/trades`; confirm `trades.side="BUY"`, `trades.is_live=true`.
9. `/kill-all`; verify SELL flatten fills; position flat in `/api/v1/live/positions`.
10. Capture all 8 items into drill report; attach to PR.

Drill is **mandatory before merge** per council verdict constraint #5. If drill fails, fix (no bugs left behind rule) and re-run.

**Commit:**

```bash
git commit -m "docs(runbooks): live-path registry wiring drill procedure + report"
```

---

## Task 16: CHANGELOG + solution doc + cleanup

**Files:**

- Modify: `docs/CHANGELOG.md`
- Create: `docs/solutions/live-trading/registry-backed-live-start.md`

**CHANGELOG entry:**

```markdown
## [unreleased] — live-path registry wiring

### Added

- `lookup_for_live(symbols, as_of_date)` pure-read resolver over
  `instrument_definitions` + `instrument_aliases`; returns typed
  `ResolvedInstrument` (options-extensible contract_spec).
- `build_ib_instrument_provider_config_from_resolved()` rebuilds
  `IBContract` from resolver output — no PHASE_1_PAPER_SYMBOLS gate.
- Structured telemetry `live_instrument_resolved` + counter
  `msai_live_instrument_resolved_total{source, asset_class}` registered
  via the project's hand-rolled `MetricsRegistry` (no `prometheus_client`
  dependency added).
- `FailureKind.{REGISTRY_MISS,REGISTRY_INCOMPLETE,UNSUPPORTED_ASSET_CLASS}`
  enum variants + distinct HTTP 422 error codes in `/start-portfolio`.
- WARN alert on registry miss, ERROR alert on incomplete row.

### Changed

- `live_supervisor`, `live_node_config`, `/api/v1/live/start-portfolio`
  all resolve via `lookup_for_live` instead of `canonical_instrument_id`.
- Non-Phase-1 symbols (QQQ, GBP/USD, NQ, GOOGL) now deployable via
  `msai instruments refresh` + deploy — no code edits needed.

### Removed (from runtime paths only)

- `canonical_instrument_id()` calls in supervisor and live_node_config.
  Helper survives for CLI/bootstrap seeding.
- `PHASE_1_PAPER_SYMBOLS` gate from the IB preload builder.

### Validated

- Real-money drill on U4705114 (QQQ or similar) 2026-04-??: registry-
  backed BUY 1 → /kill-all → flat. Drill report:
  `docs/runbooks/drill-reports/2026-04-??-live-path-registry-drill.md`.
```

**Solution doc:** a short post-incident-style writeup capturing the architecture + the two-layer-registry fix (schema + alias windowing) for future reference.

**Quality gates:**

```bash
cd backend && uv run ruff check src/
cd backend && uv run mypy src/ --strict
cd backend && uv run pytest tests/ -v
```

Expected: all clean. Stop if any fails — no bugs left behind.

**Commit:**

```bash
git commit -m "docs: CHANGELOG + solution writeup for live-path registry wiring"
```

---

## Resolved in iter-1 plan-review (2026-04-20)

Iter-1 (Claude + Codex) surfaced 0–3 P0 / 7 P1 / 2–5 P2 blocking findings. All addressed in this v2:

| Finding                                                                                         | Resolution                                                                                                                                                                                            |
| ----------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `send_alert` API drift (no module-level symbol; wrong kwargs)                                   | Task 8 now uses `alerting_service.send_alert(level, title, message)` — sync, no `await`, no `context`.                                                                                                |
| Prometheus counter pattern (project uses hand-rolled registry, not `prometheus_client`)         | Task 7 uses `_r.counter(name, help)` + `.labels(**kwargs).inc()`.                                                                                                                                     |
| `lookup_for_live` missing bare-ticker branch                                                    | Task 3 branches on `"." in sym`: dotted → `find_by_alias`; bare → `find_by_raw_symbol`.                                                                                                               |
| `_parse_futures_expiry` decade boundary (2029 → H0 became 2020, not 2030)                       | Task 2 now picks the smallest year ≥ `effective_from.year` whose units digit matches. New tests for 2029→2030 + in-decade baseline.                                                                   |
| Overlapping alias windows non-deterministic                                                     | Task 3's `_pick_active_alias` sorts `effective_from DESC` and WARN-logs when `overlap_count > 1` (PRD §4 US-003 tie-break rule).                                                                      |
| Aliases relationship returns ALL providers (no provider filter in manual walk)                  | `_pick_active_alias` filters `a.provider == provider`.                                                                                                                                                |
| Task 9 supervisor failure path (wrong contract: `publish_failure` doesn't exist)                | Task 9 rewritten: resolver errors subclass `ValueError` and raise out of payload factory. `ProcessManager` dispatches on subtype in the permanent-catch and calls `_mark_failed(failure_kind=…)`.     |
| Task 12 API preflight out of scope per PRD/council                                              | Task 12 rewritten: extend `FailureKind` enum + `EndpointOutcome` mapping. No preflight. Error classification flows through existing `live_node_processes` poll.                                       |
| Task 13 grep brittle                                                                            | Task 13 now AST-based — walks `Import`/`ImportFrom`/`Name`/`Attribute` nodes; catches aliased imports + attribute access + re-exports.                                                                |
| Task 4 "may already pass" is not TDD                                                            | Task 4 reframed as a characterization test locking Task 3's aggregation invariant.                                                                                                                    |
| Task 5 skipped corrupt-row integration test                                                     | Replaced with monkey-patched `_build_contract_spec` that raises; asserts `lookup_for_live` propagation + (Task 8) alerting path.                                                                      |
| TradingNodePayload pickle round-trip for `resolved_instruments` not tested                      | New **Task 11b** — explicit `pickle.dumps/loads` test plus `mp.get_context("spawn")` pathway note.                                                                                                    |
| Fixture placeholders (`db_session`, `auth_headers`) don't exist in root `conftest.py`           | All tests now follow the per-module `session_factory` + `isolated_postgres_url` pattern from `test_security_master_resolve_live.py:38-54`. API tests reuse `test_live_start_endpoints.py`'s `client`. |
| HTTPException double-wrapping (`detail={"error":...}` → response is `{"detail":{"error":...}}`) | Task 12 uses `JSONResponse(status_code=422, content={"error": {...}})`.                                                                                                                               |
| FX currency non-USD quote / malformed `raw_symbol`                                              | `_build_contract_spec` validates both `base` and `quote` non-empty; tests cover `EUR/USD` (USD quote) + leaves non-USD quote as documented future work (council non-goal for this PR).                |

## Resolved in iter-2 plan-review (2026-04-20)

Iter-2 review (Claude 0 P0 / 5 P1 / 4 P2; Codex 0 P0 / 4 P1 / 1 P2). All addressed in iter-3 plan revision:

| Finding                                                                                                   | Resolution                                                                                                                                                                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EndpointOutcome` contract wrong (targeted non-existent module; 503/`detail` vs. plan's 422/`{error:{}}`) | Task 12 rewritten against real `services/live/idempotency.py:201-240`. New `registry_permanent_failure(kind, error_message)` factory; legacy `permanent_failure` unchanged. New `_REGISTRY_FAILURE_KINDS` set.                                                                                    |
| Function name `build_live_node_config` doesn't exist                                                      | Task 11 rewritten against real `build_portfolio_trading_node_config` at `live_node_config.py:416`. Aggregates `member.resolved_instruments` dedup'd by `canonical_id`.                                                                                                                            |
| Sync `alerting_service.send_alert` blocks event loop                                                      | Task 8 wraps in `await asyncio.to_thread(alerting_service.send_alert, ...)` — matches existing production pattern at `alerting.py:313-314`.                                                                                                                                                       |
| PRD US-002 `details.missing_symbols` self-contradiction                                                   | Resolver errors now expose `to_error_message()` → JSON envelope `{code, message, details}`. `ProcessManager._mark_failed` persists that JSON in `error_message` column. Factory parses back. PRD acceptance met.                                                                                  |
| `AmbiguousSymbolError` not caught (bare `Exception`, lands in transient-retry branch)                     | New `AmbiguousRegistryError(LiveResolverError)` wraps it at the resolver boundary so supervisor's permanent-catch fires. `ProcessManager` dispatch maps to `FailureKind.AMBIGUOUS_REGISTRY` (distinct kind added in iter-4, NOT REGISTRY_INCOMPLETE). Task 3b exposes `.asset_classes` attribute. |
| `_pick_active_alias` tie-break used random UUID `.id`                                                     | Sort key now `(effective_from, alias_string)` reversed — `alias_string` is business-stable (canonical dotted form).                                                                                                                                                                               |
| `find_by_alias` UTC default not removed                                                                   | Task 3b removes the default — `as_of_date: date` becomes required. Existing callers (`resolve_for_live`, `resolve_for_backtest`) already pass it; signature-lock test added.                                                                                                                      |
| Test fixture Task 7/8 underspecified                                                                      | Split into integration file (uses per-module `session_factory` + `isolated_postgres_url` fixtures from Task 3) + unit file for counter-only tests (no DB).                                                                                                                                        |
| Empty-symbols bare `ValueError`                                                                           | Task 9 supervisor payload factory adds a defensive guard BEFORE calling resolver — "strategy member has no instruments" with clear message.                                                                                                                                                       |
| Task 11b pickle test placeholders                                                                         | Uses existing `_make_member()` helper + documents extending it with `resolved_instruments=()` kwarg default.                                                                                                                                                                                      |
| Error-message prefix pollution (`"payload factory failed (permanent): "`)                                 | Task 9 dispatch branches: `LiveResolverError` → `reason = exc.to_error_message()` (clean JSON); everything else → existing prefixed format.                                                                                                                                                       |
| Databento alias co-existence not checked                                                                  | Task 15 drill pre-flight adds SQL check for `provider='databento'` rows.                                                                                                                                                                                                                          |

---

## Open spot checks (iter-3 plan-review)

Final open questions after two iterations of reviewer feedback:

1. **AmbiguousSymbolError attribute order (Task 3b)** — the new constructor takes `(symbol, provider, asset_classes)` positionally. Existing call site at `registry.py:112` currently uses `AmbiguousSymbolError(f"...")` with one positional string. Updating that call site is part of Task 3b; confirm no downstream tests rely on the legacy string-only form.
2. **Backward compat of `registry_permanent_failure` parse-fallback** — if an old cached response exists in Redis from a pre-PR `_mark_failed` call, its `error_message` will be plain text. The factory handles this (returns code = `REGISTRY_MISS`/etc. upper-case from enum, message = raw string, details = {}). But is that acceptable HTTP behavior, or should we invalidate pre-PR cached responses on deploy?
3. **`_REGISTRY_FAILURE_KINDS` export surface** — the endpoint handler imports it from `idempotency.py` (not a public API). Confirm the `__all__` or module-public convention of this project doesn't require exposing it.
4. **Futures-roll counter label cardinality** — `LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(source=X, asset_class=Y)` — `asset_class` has 5 values (equity/futures/fx/option/crypto), `source` has 2-3 (registry, registry_miss, registry_incomplete). 15 max time-series = fine for Prometheus. No cardinality concerns.
5. **Non-USD FX quote scope** — explicit defer. PRD §2 non-goals already covers options + crypto; add an explicit FX-quote-currency-USD-only note before merge if not already there.

---

## Execution Handoff

**Plan complete and saved to `docs/plans/2026-04-20-live-path-wiring-registry.md`.** Two execution options:

**1. Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Stays in this session.

**2. Parallel Session (separate)** — Open a new session in the worktree with `superpowers:executing-plans`, batch execution with checkpoints.

Next actual step per CONTINUITY.md checklist is **Phase 3.3 plan-review loop** (Claude + Codex in parallel; exit when no P0/P1/P2 on the same pass) — the execution choice lands after plan-review converges.
