# /symbols/readiness 422 dead-end for dual-provider symbols (unsatisfiable "pin provider explicitly")

**Status:** fixed on `fix/symbols-readiness-provider-param` (2026-06-06). E2E PASS (API + CLI).
**Observed:** 2026-06-05 regression sweep — `GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity` → `422 AMBIGUOUS_INSTRUMENT "…pin provider explicitly"` for every symbol registered under both `databento` and `interactive_brokers` (the normal state after IB qualification) — and the endpoint had **no `provider` parameter**, so the instruction was unsatisfiable. Blocked 3 graduated UCs.

## Problem

The registry stores one `InstrumentDefinition` per `(symbol, provider, asset_class)`. `find_active_aliases` collapsed alias rows by `instrument_uid` and raised on >1 — making every dual-provider symbol _structurally_ ambiguous. The readiness endpoint surfaced that as a 422 whose own recovery instruction couldn't be followed. **Worse sibling:** `DELETE /symbols/{symbol}` called the same method with **no `AmbiguousSymbolError` handler at all** — dual-provider deletes bubbled as an unhandled **500** (found by plan review; the regression sweep had only seen the readiness face of it).

## Root Cause

- `service.py` `find_active_aliases`: uid-collapse + raise, no provider filter; the "richer context" `providers=[…]` had been added to _describe_ ambiguity, never to _resolve_ it (`registry.py` docstring admitted "handlers don't expose that yet").
- **Key insight that shaped the fix:** `compute_coverage` is keyed on `(asset_class, symbol)` — the Parquet layout has **no provider dimension**, so the data-readiness answer is provider-invariant. Only the metadata block (uid/provider/live_qualified) differs per provider. Raising 422 for an invariant answer was gratuitous.

## Solution (approach C — contrarian-validated)

- `find_active_aliases` gains `provider: Provider | None` (WHERE filter) and `on_ambiguity: Literal["raise","prefer_primary"]`; the preference policy (`databento` > `interactive_brokers` > lexicographic) was extracted to `_preferred_provider` and reused, with a direct unit test pinning preference-over-lexicographic (a 2-provider seed alone cannot distinguish them — `sorted()` gives the same answer).
- `GET /symbols/readiness`: optional `provider` query param; **unpinned dual-provider now answers 200 with the preferred primary named**; pinned returns that provider's view (`live_qualified` scoped); pinned-with-no-row → 404 **naming the provider**.
- `DELETE /symbols/{symbol}`: same param; unpinned ambiguity **stays 422** (destructive ops stay explicit) via a NEW `AmbiguousSymbolError` handler (replacing the unhandled 500); pinned delete is provider-scoped (sibling row survives — pinned by a test that verifies through the inventory endpoint).
- `msai symbols readiness|delete` CLI: `--provider` option (ProviderOption StrEnum) — found by code review; the plan's surface audit had missed `cli_symbols.py` because it grepped only `cli.py`'s sub-app block (the mount is at `cli.py:3011`).
- `how-symbols-work.md`: full line-cite sweep (~25 corrections incl. a second drifted appendix table) + §4.6 documenting the new behavior.

## Prevention

- **Never ship an error message whose instruction the same interface cannot satisfy.** If an error says "pin X", the parameter to pin X must exist on that surface (all surfaces — API _and_ CLI).
- **Read vs destructive asymmetry:** when an answer is invariant across the ambiguous dimension, reads should resolve via a documented preference; destructive ops should stay explicit.
- **Surface audits must grep for command modules, not just the sub-app registry list** — `cli_symbols.py` was invisible to a `cli.py` sub-app grep.
- **Preference-policy tests need a discriminating case:** with only two members sorted lexicographically in preference order, `sorted()[0]` masquerades as policy — unit-test the helper with a hypothetical third member.
