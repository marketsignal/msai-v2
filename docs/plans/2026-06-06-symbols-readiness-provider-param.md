# Fix: /symbols/readiness provider dead-end (AMBIGUOUS_INSTRUMENT unsatisfiable) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /api/v1/symbols/readiness` must answer for dual-provider symbols instead of dead-ending: an optional `provider` query param pins resolution, and the unpinned READ defaults to the existing databento>IB preference (coverage is provider-invariant), while the destructive `DELETE /symbols/{...}` keeps its explicit-provider requirement with a now-satisfiable error.

**Architecture:** Approach C (contrarian-validated iter 1): thread an optional `provider` filter through `find_active_aliases` (its ONLY two callers are the readiness GET and the remove DELETE); on the readiness path, replace the unpinned dual-provider 422 with preference-ordered primary selection (`databento` > `interactive_brokers`, the policy already at `service.py:1029-1034`) — response continues to name the resolved `provider`, `has_ib_alias` stays aggregate-across-providers when unpinned and provider-scoped when pinned; on the remove path, ambiguity without an explicit `provider` still 422s (destructive ops stay explicit) but the error is now actionable.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async, pytest + testcontainers (Postgres), existing symbol-onboarding integration harness.

---

## Root Cause (verified, live-reproduced on main 2026-06-05)

1. `find_active_aliases` (`backend/src/msai/services/nautilus/security_master/service.py:931-1041`) selects every active alias for `(raw_symbol, asset_class)` and collapses by `instrument_uid` (`:1003`); `len(uids) > 1` → `AmbiguousSymbolError(providers=…)` (`:1004-1025`).
2. The registry stores ONE `InstrumentDefinition` per `(symbol, provider, asset_class)` — dual-provider symbols (AAPL/MSFT/SPY today) are therefore _structurally always_ ambiguous to this method.
3. The readiness endpoint (`backend/src/msai/api/symbol_onboarding.py:624-722`) maps that to `422 AMBIGUOUS_INSTRUMENT … "pin provider explicitly"` (`:656-675`) — but its signature exposes only `symbol`, `asset_class`, `start`, `end`. The instruction is unsatisfiable.
4. Reproduced live: `GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity` → `422 {"error":{"code":"AMBIGUOUS_INSTRUMENT","message":"Symbol 'SPY' … matches definitions under multiple providers (['databento', 'interactive_brokers']); pin provider explicitly."}}` while `/symbols/inventory` returns the same symbol cleanly as one row per provider (`service.py:854-929` groups by provider).
5. **Provider-invariance of the data answer:** `compute_coverage` (`backend/src/msai/services/symbol_onboarding/coverage.py`) is keyed purely on `(asset_class, symbol)` — Parquet layout has no provider dimension. Only the metadata block (uid/provider/live_qualified) varies by provider.
6. **Sibling latent dead-end (worse):** `remove_symbol` DELETE calls the same `find_active_aliases` (`symbol_onboarding.py:838`) **with NO `AmbiguousSymbolError` handler** — soft-deleting a dual-provider symbol bubbles as an **unhandled 500** (plan-review iter-1 P1; only readiness maps the error to 422).
7. The gap was known-but-unfixed: `registry.py:46` docstring says handlers "don't expose that yet, so they raise with this richer context instead".

## Approach Comparison (final)

### Chosen Default

**C — provider param + read-default-to-primary, destructive stays explicit** (described in Architecture above).

### Best Credible Alternative

**A — param-only:** same param plumbing; unpinned dual-provider keeps 422 everywhere.

| Axis                  | C (default)                                                                                               | A                            | B (default-only, no param)               |
| --------------------- | --------------------------------------------------------------------------------------------------------- | ---------------------------- | ---------------------------------------- |
| Complexity            | Low-medium                                                                                                | Low                          | Low                                      |
| Blast Radius          | 2 endpoints + 1 service fn (only 2 callers) + tests + stale-doc fix                                       | same minus default branch    | 1 endpoint; no pinning                   |
| Reversibility         | Trivial (additive param; default branch revertible)                                                       | Trivial                      | Trivial                                  |
| Time to Validate      | Fast (PG-testcontainers integration; API-only E2E)                                                        | Fast                         | Fast                                     |
| User/Correctness Risk | Default-pick masks "which uid?" — mitigated: coverage provider-invariant + response names picked provider | Dead-end persists by default | Cannot pin per-provider metadata; weaker |

## Contrarian Verdict

**VALIDATE (iteration 1, Codex gpt-5.5):** "Coverage is provider-invariant, `live_qualified` is already aggregate IB-alias semantics, and adding `provider` while defaulting only the read path fixes the user dead-end without weakening explicit destructive deletes."

## File Structure

| File                                                                | Change                                                                                                                                                                                                                       |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/src/msai/services/nautilus/security_master/service.py`     | `find_active_aliases` gains `provider: Provider \| None = None` filter + `on_ambiguity: Literal["raise","prefer_primary"]` behavior knob (see Task 1 for exact shape)                                                        |
| `backend/src/msai/api/symbol_onboarding.py`                         | readiness GET: add `provider` Query param, pass through, request `prefer_primary` when unpinned; remove DELETE: add `provider` Query param, keep `raise` semantics                                                           |
| `backend/tests/integration/api/test_symbol_onboarding_readiness.py` | dual-provider seeds + new tests (unpinned default, pinned each provider, pinned-missing 404 NOT_FOUND provider-named, remove-ambiguous 422 + pinned remove)                                                                  |
| `backend/src/msai/services/nautilus/security_master/registry.py`    | update the stale `:46` "handlers don't expose that yet" docstring (iter-1 P3)                                                                                                                                                |
| `docs/architecture/how-symbols-work.md`                             | fix the three stale/wrong claims (`:431` wrong line-cite for preference policy; `:439`/`:473` wrong ambiguity description) + document the new param/default                                                                  |
| `backend/src/msai/cli_symbols.py`                                   | `readiness` + `delete` commands gain an optional `--provider [databento\|interactive_brokers]` option (forwarded as the `provider` query param); the delete docstring notes the satisfiable 422 path (code-review iter-1 P2) |

`Provider` literal already exists (`types.py:22`). `ReadinessResponse` schema unchanged (already carries `provider: str`). Only two callers of `find_active_aliases` — both updated here.

---

#### E2E Use Cases

**Surface coverage decision** (per `CLAUDE.md ## E2E Configuration`, surfaces `[API, CLI, UI]`):

- **API: Covered** — UC1 below. `GET /symbols/readiness` is a public/product API surface (operator/integrator-facing; documented in `how-symbols-work.md` as the Phase-1 read-through surface).
- **CLI: Covered** — UC2 below. The `msai symbols` sub-app (mounted `cli.py:3011`) already exposes `readiness` and `delete` commands that call these endpoints; both gained the optional `--provider [databento|interactive_brokers]` option in this fix so a CLI operator can pin a dual-provider symbol's readiness view and satisfy the destructive-delete disambiguation (code-review iter-1 P2 — the CLI was left behind in the original plan, which predated the discovery that `cli_symbols.py` already wraps these routes).
- **UI: N/A** — the frontend consumes `/symbols/inventory` (`frontend/src/components/live/instrument-readiness-check.tsx:44`), never `/symbols/readiness` (grep clean); inventory already disambiguates per provider and is untouched by this fix.

**UC1 — API: data-readiness answer for a symbol carried by both providers**

```
Actor:         API integrator (operator tooling) checking whether a symbol's historical data
               is ready before scheduling a backtest
Scenario:      Their registry carries SPY under both databento and interactive_brokers (the
               normal state after IB qualification). Yesterday the readiness check dead-ended
               with 422 "pin provider explicitly" — an instruction the API gave no way to
               follow. They need the plain data-readiness answer, and the per-provider view
               when they ask for it.
Interface:     API
Intent:        The integrator asks "is SPY's data ready?" and gets a direct answer, and can
               pin a specific provider's view of the same symbol when they need it.
Setup:         Dev stack up from this worktree. SPY registered under BOTH providers (the
               standing dev-registry state — verify via GET /api/v1/symbols/inventory; if
               absent, register via the documented bootstrap: POST /api/v1/instruments/bootstrap
               for databento + msai instruments refresh --provider interactive_brokers).
               (Do NOT call readiness in Setup — that's the action under test.)
Steps:         1) GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity   (unpinned)
               2) GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity&provider=interactive_brokers
Verification:  Step 1 receives 200 — the response includes coverage_status/backtest_data_available
               for SPY plus the resolved provider name (databento, the preferred primary) —
               NOT the former 422 dead-end; the integrator can act on it (schedule the backtest
               or trigger ingest for the missing ranges it names). Step 2 receives 200 with
               provider=interactive_brokers metadata for the SAME symbol and the same
               coverage answer, demonstrating the pin works.
Persistence:   Re-request step 1 after a short delay — same 200 shape, same resolved provider
               (deterministic preference, not a coin flip). A provider value outside the
               registry (e.g. provider=interactive_brokers on a databento-only symbol) still
               returns an explanatory 4xx the integrator can correct.
```

**UC2 — CLI: operator checks a symbol's data-readiness from the shell, then pins a provider**

```
Actor:         Operator at the shell sizing up whether a symbol's historical data is ready
               before kicking off an overnight backtest
Scenario:      Their registry carries SPY under both databento and interactive_brokers (the
               normal post-IB-qualification state). They want a quick readiness answer from
               the terminal without opening the UI, and then the interactive_brokers-specific
               view when they need to confirm live qualification for that provider.
Interface:     CLI
Intent:        The operator asks "is SPY's data ready?" from the CLI and gets a direct answer,
               then pins interactive_brokers to see that provider's view of the same symbol.
Setup:         Dev stack up from this worktree (backend reachable on :8800, MSAI_API_KEY set
               for the CLI). SPY registered under BOTH providers — verify via
               `msai symbols inventory`; if absent, register via the documented bootstrap
               (POST /api/v1/instruments/bootstrap for databento + `msai instruments refresh
               --provider interactive_brokers`). (Do NOT run `msai symbols readiness` in
               Setup — that's the action under test.)
Steps:         1) Run `msai symbols readiness --symbol SPY --asset-class equity`   (unpinned)
               2) Run `msai symbols readiness --symbol SPY --asset-class equity --provider interactive_brokers`
Verification:  Step 1 exits 0 and stdout shows a JSON readiness block whose `provider` field
               reads `databento` (the server's preferred primary) with `registered: true` —
               NOT the former 422 dead-end echoed to stderr; the operator can act on the
               coverage_status/missing_ranges it names. Step 2 exits 0 and stdout shows the
               same block but with `provider: interactive_brokers` for the SAME symbol,
               demonstrating the pin reached the API.
Persistence:   Open a fresh shell and re-run step 1 — stdout shows the same `provider:
               databento` block (deterministic preference, not a coin flip), confirming the
               answer is stable across invocations. Running with `--provider bogus` is
               rejected by the CLI before any call (non-zero exit, choices listed in the
               error), and `--provider interactive_brokers` against a databento-only symbol
               returns a 4xx the operator can read on stderr and correct.
```

---

## Developer Briefing (Gate 1)

**What I'll fix:** Asking "is this symbol's data ready?" currently errors out for any symbol known to both data providers — and the error tells you to do something the API doesn't allow. After the fix the question just gets answered (the data answer doesn't depend on provider anyway), and a new optional `provider` filter lets you ask for one provider's view explicitly. Deleting a symbol stays deliberately strict — but the strictness is now satisfiable.

**Planned file-map** `[planned]`: `service.py` (find_active_aliases filter+knob), `api/symbol_onboarding.py` (2 endpoints incl. the NEW remove-path ambiguity handler), `registry.py` (stale docstring), readiness integration tests, `how-symbols-work.md`.

**Key decisions:** read defaults to the existing databento>IB preference because coverage is provider-invariant `[verified: coverage.py keyed on (asset_class, symbol)]`; destructive delete keeps explicit-provider 422 `[planned]`; `has_ib_alias` aggregate when unpinned / scoped when pinned `[planned]`.

---

### Task 1: `find_active_aliases` provider filter + ambiguity knob

**Files:**

- Modify: `backend/src/msai/services/nautilus/security_master/service.py:931-1041`
- Test: `backend/tests/integration/api/test_symbol_onboarding_readiness.py` (service-level assertions ride through the endpoint tests in Task 2; this task's red/green runs the new dual-provider endpoint test for the unpinned default)

- [ ] **Step 1: Write failing tests (in the readiness endpoint test file — they drive both tasks)**

Read `backend/tests/integration/api/test_symbol_onboarding_readiness.py` first — reuse its harness (`_seed_active_alias` helper, PG testcontainer fixtures). Add a dual-provider seed helper (`_seed_dual_provider(symbol)` seeding BOTH a databento and an interactive_brokers definition+alias for the same symbol/asset_class — mirror how `test_instrument_registry.py:152` seeds coexisting provider rows) and these tests:

```python
async def test_readiness_dual_provider_unpinned_returns_primary():
    """THE BUG: unpinned readiness on a dual-provider symbol must answer 200
    with the preferred primary (databento) named — pre-fix this 422'd with an
    unsatisfiable 'pin provider explicitly'."""
    # seed dual-provider SPY; GET readiness without provider
    # assert 200; body["provider"] == "databento"; body has coverage fields;
    # body["live_qualified"] is True (aggregate: IB alias exists across providers)

async def test_readiness_dual_provider_pinned_ib_returns_ib_view():
    # GET readiness with provider=interactive_brokers
    # assert 200; body["provider"] == "interactive_brokers";
    # body["live_qualified"] True (scoped: the IB row itself qualifies);
    # coverage fields identical to the unpinned call (provider-invariant)

async def test_readiness_dual_provider_pinned_databento_scopes_live_qualified():
    # GET readiness with provider=databento on the dual-provider seed
    # assert 200; body["provider"] == "databento";
    # body["live_qualified"] is False (SCOPED: the databento row alone has no
    # IB alias — contrast with the unpinned aggregate True)

async def test_readiness_pinned_provider_with_no_row_is_actionable_4xx():
    # seed databento-only symbol; GET readiness with provider=interactive_brokers
    # assert 4xx (404 NOT_REGISTERED envelope per the endpoint's existing
    # not-found mapping — confirm the actual mapping when reading the endpoint);
    # error body names the symbol and provider so the caller can correct

async def test_remove_dual_provider_unpinned_is_satisfiable_422_not_500():
    # DELETE the symbol unpinned on a dual-provider seed
    # PRE-FIX BEHAVIOR (plan-review iter-1 P1 finding): remove_symbol has NO
    # AmbiguousSymbolError handler today (only readiness maps it) — the error
    # bubbles as an unhandled exception → 500. The sibling bug is therefore a
    # 500, not a 422.
    # assert post-fix: 422 AMBIGUOUS_INSTRUMENT envelope; message instructs
    # pinning — and the instruction is now satisfiable via the provider param

async def test_remove_dual_provider_pinned_deletes_only_that_provider():
    # DELETE with provider=interactive_brokers → success per the endpoint's
    # existing delete contract; then GET /symbols/inventory shows the databento
    # row REMAINS and the IB row is gone (provider-scoped delete)
```

(Adapt assertion details to the endpoint's real response shapes — read them; the CONTRACT is: unpinned read answers with primary; pinned read scopes; pinned-missing is an actionable 4xx; unpinned destructive stays 422; pinned destructive scopes.)

- [ ] **Step 2: Run → confirm red** (`cd backend && uv run python -m pytest tests/integration/api/test_symbol_onboarding_readiness.py -q`): expected RED reasons (plan-review iter-1 P3 correction — FastAPI IGNORES undeclared query params, so none of these fail with "unknown param"): unpinned readiness test fails 422-instead-of-200 (the bug); pinned readiness tests fail via the SAME ambiguity 422 (param silently ignored pre-fix); pinned-missing test likely gets 200-instead-of-4xx (param ignored → resolves the databento row); unpinned remove test fails with the unhandled-exception 500 (the true pre-fix sibling behavior); pinned remove test fails via the 500 too. The three pre-existing tests stay green.

- [ ] **Step 3: Implement the service change**

In `find_active_aliases`, add keyword-only params:

```python
provider: Provider | None = None,
on_ambiguity: Literal["raise", "prefer_primary"] = "raise",
```

- When `provider` is not None: add `InstrumentDefinition.provider == provider` to the row-select WHERE; a result of zero rows follows the existing not-found path; the uid-collapse then can't be provider-ambiguous.
- When unpinned and `len(uids) > 1`:
  - `on_ambiguity == "raise"` → existing `AmbiguousSymbolError` (unchanged — remove path).
  - `on_ambiguity == "prefer_primary"` → choose the uid whose definition's provider ranks first under the EXISTING preference order (reuse/extract the `databento` > `interactive_brokers` > sorted-first logic at `service.py:1029-1034` — do NOT duplicate it; factor a small helper if needed), restrict the alias rows to that uid for the metadata block, but compute `has_ib_alias` across ALL the symbol's active alias rows (aggregate semantics preserved).
- When pinned: `has_ib_alias` scopes to the pinned provider's rows.
- Docstring: document the knob + cite this fix. The stale "handlers don't expose that yet" remark lives in `registry.py:46` (NOT service.py — plan-review iter-1 P3 correction): update it there.

- [ ] **Step 4: Implement the endpoint changes** (`backend/src/msai/api/symbol_onboarding.py`)

- `readiness` (`:624-722`): add `provider: Provider | None = Query(default=None)`; call `find_active_aliases(..., provider=provider, on_ambiguity="prefer_primary")`. Keep the `AmbiguousSymbolError` handler as a defensive fallback (it should now be unreachable from this path — note that in a comment rather than deleting the handler). **404 message branch (iter-1 P2):** when `provider is not None`, the not-found message (current shape at `:676`) must NAME the pinned provider (e.g. "Symbol 'X' (asset_class='equity') is not registered under provider 'interactive_brokers'") — keep the existing error `code` unchanged.
- `remove_symbol` (`:818-856`): add the same Query param; call with `provider=provider, on_ambiguity="raise"`. **There is NO ambiguity handler here today (iter-1 P1)** — `AmbiguousSymbolError` currently escapes as an unhandled 500. ADD the same `try/except AmbiguousSymbolError` → 422 `AMBIGUOUS_INSTRUMENT` envelope used by readiness (`:656-675` shape), so the unpinned dual-provider DELETE becomes a satisfiable 422. Apply the same provider-naming branch to its not-found message (`:843`).

- [ ] **Step 5: Run → green** (same pytest command): all new + 3 pre-existing pass. Then `uv run ruff check` on both source files + `uv run mypy src/msai/services/nautilus/security_master/service.py src/msai/api/symbol_onboarding.py --strict`.

- [ ] **Step 6: No commit yet** (workflow gate) — stage files.

### Task 2: stale architecture doc fix

**Files:** Modify: `docs/architecture/how-symbols-work.md:431, 439, 473`

- [ ] Correct `:431` (preference policy actually lives at `service.py:1029-1034`, not `:778-783` — re-verify the post-change line numbers and cite those), `:439` and `:473` (ambiguity fires on same-asset_class dual-provider rows with NO metadata conflict — rewrite to describe the real trigger), sweep the WHOLE doc for stale `find_active_aliases` / preference-policy line references (e.g. `:642` flagged by plan review — re-verify every service.py line-cite against the post-Task-1 code), and add a short subsection documenting the new `provider` param + unpinned-read default + explicit-delete asymmetry. Stage; no commit.

---

## Dispatch Plan

| Task                                | Writes                                                                          | Depends on                              | Mode   |
| ----------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------- | ------ |
| Task 1: service + endpoints + tests | `service.py`, `api/symbol_onboarding.py`, `test_symbol_onboarding_readiness.py` | —                                       | serial |
| Task 2: doc fix                     | `docs/architecture/how-symbols-work.md`                                         | Task 1 (cites post-change line numbers) | serial |

Concurrency: 1. Failure semantics: Task 1 failure cancels Task 2.

## Operational notes

- The three blocked graduated UCs (`market-data/uc-cdp-002`, `uc-cdp-004`, `instruments/UC-SYM-004`) become runnable unpinned after this fix — they are part of the FAIL_STALE maintenance sweep (separate item) but uc-cdp-002/004 may simply pass as-written once the dead-end is gone; verify-e2e regression will tell.
- Additive API change only; no migration; no consumer breaks (param optional, success shape unchanged; the only behavior change is 422→200 on a path that was a dead-end).

## Self-review notes

- Spec coverage: dead-end fix (T1), pin capability (T1), destructive asymmetry (T1), aggregate-vs-scoped live_qualified (T1 step 3), stale docs (T2), no-dual-provider-test gap closed (T1 step 1). ✓
- Delegated-detail blocks (assertion shapes) carry read-first instructions with the contract frozen in prose. ✓
- Type consistency: `Provider` literal reused from `types.py:22` in both service kwarg and Query param. ✓
