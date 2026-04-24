# Decision: Databento instrument-registry bootstrap for equities & ETFs

**Date:** 2026-04-23
**Status:** **FINAL — council-ratified** (5 advisors + Codex xhigh chairman)
**Predecessors:**

- PR #32 (db-backed instrument registry schema + `SecurityMaster.resolve_for_backtest`)
- PR #35 (`msai instruments refresh --provider interactive_brokers` IB CLI)
- PR #37 (live-path wiring onto registry — `docs/decisions/live-path-registry-wiring.md`)
- PR #40 (backtest auto-ingest on missing data — made backtests registry-gated)

**Related user north-star (verbatim 2026-04-23):** "I want to be able to run backtests and add symbols and run backtests and then graduate them and go into production. I won't be able to use any instruments and create strategies for any instrument, any time frame, any bar (1-minute, 5-minute, 1-hour bar) of any asset type, and iterate quickly and see where I can find alpha before I can graduate it to a portfolio and then to live trading."

---

## TL;DR

Ship an on-demand Databento bootstrap path so equities and ETFs no longer require IB Gateway running to be added to the registry. The contract is explicit: a Databento-bootstrapped row is **backtest-discoverable only**. Live graduation still requires an explicit `msai instruments refresh --provider interactive_brokers` second step. Use a new verb (`bootstrap`), not a fourth overloading of `refresh`. No fixed seed list, no recurring catalog sync, no IB replacement, no silent enrichment, no options in v1.

---

## Verdict: `1b + 2b + 3a + 4a`

| #   | Question           | Verdict                                             | Rationale                                                                                                                                                                                                                                                                                           |
| --- | ------------------ | --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Shape of bootstrap | **1b** — arbitrary-symbol on-demand CLI/API         | Unanimous. Fixed seed defeats the iteration loop; catalog-sync cron undoes PR #37's operator-managed registry decision.                                                                                                                                                                             |
| 2   | Coverage           | **2b** — equities + ETFs + futures (NO options)     | 3-2 majority. Equities/ETFs are the new work. Existing futures continuous-contract path stays (conditional on fixing `BE-01` first OR calling futures out as existing-path-only). Options deliberately excluded — Nautilus gotcha #12 (SPY alone = thousands of strikes) belongs in a separate PRD. |
| 3   | Relationship to IB | **3a** — Databento and IB are peers on the same CLI | 3-2 majority. Databento-bootstrapped row = backtest-discoverable only. Live graduation requires an explicit IB refresh. No silent enrichment, no Databento-replaces-IB.                                                                                                                             |
| 4   | Cost posture       | **4a** — metered-mindful                            | 4-1 majority. Registry writes sit on the live-path control plane; can't YOLO metadata queries. Retry/backoff + concurrency caps + usage counter required in v1.                                                                                                                                     |

---

## 7 Blocking Constraints (must be satisfied by the implementation PR)

1. **Rate-limit and usage safety.** `databento_client.py` must ship with retry/backoff (`tenacity`), a `--max-concurrent` cap, and a `msai_databento_api_calls_total{endpoint,outcome}` counter. No fire-and-forget batch of 100 symbols that 429s mid-batch leaving a partial registry.

2. **Write-path correctness.** The Databento `_upsert_definition_and_alias` path requires advisory locking (`pg_advisory_xact_lock(hashtext(provider || raw_symbol || asset_class))` or `SELECT ... FOR UPDATE` on the definition row) to prevent concurrent alias-close/insert races. PR #32 postscript `8f5f943` fixed this for IB rolls; the bootstrap path opens a second writer and needs the same protection.

3. **Divergence observability.** v1 must emit `msai_registry_bootstrap_total{provider,asset_class,outcome}` and `msai_registry_venue_divergence_total{databento_venue,ib_venue}` counters, plus a structured `registry_bootstrap_divergence` log event when IB enrichment later disagrees with a Databento-authored venue or alias. Without this, the first IB-vs-Databento venue rename (e.g., an ETF migrates ARCA→BATS) is a silent live-deploy failure with no telemetry.

4. **Semantics: "backtest-discoverable only."** Databento-bootstrapped registry rows make the backtest discoverable. They do NOT imply live-readiness. Any CLI/API wording that implies live qualification is blocking. The `--help` text and the PRD must say this explicitly.

5. **No recurring catalog-sync cron in v1.** Undoes the 2026-04-20 PR #37 operator-managed registry decision. Symbol drift (corporate actions, venue migrations) surfaces via the divergence counter in #3; operator runs `bootstrap` or `refresh` manually when it fires.

6. **End-to-end acceptance bar.** v1 is not complete without a passing demo for `AAPL`, `SPY`, and `QQQ`: Databento seed → registry hit → auto-heal ingest → backtest start. Row creation alone is NOT a passing test. The E2E use case enforces this.

7. **Distinct verb — no `refresh` overloading.** Command is `msai instruments bootstrap --provider databento --symbols SPY,AAPL,QQQ`. `refresh` remains "re-qualify/warm an existing provider path" (IB qualification, Databento continuous-futures synthesis). The ambiguity contract for multi-candidate equity symbols (e.g., `BRK.B`) must be specified in the PRD: exception type + CLI/API message shape + retry guidance.

---

## Advisor tally

| Advisor              | Engine | Q1           | Q2           | Q3           | Q4           | Verdict                      |
| -------------------- | ------ | ------------ | ------------ | ------------ | ------------ | ---------------------------- |
| The Simplifier       | Claude | 1b           | 2a           | 3a           | 4b           | APPROVE                      |
| The Scalability Hawk | Claude | 1b           | 2b           | 3b           | 4a           | CONDITIONAL                  |
| The Pragmatist       | Claude | 1b           | 2a           | 3a           | 4a           | APPROVE                      |
| The Contrarian       | Codex  | 1b           | 2b           | 3a           | 4a           | CONDITIONAL                  |
| The Maintainer       | Codex  | 1b           | 2b           | 3b           | 4a           | CONDITIONAL                  |
| **Verdict**          | —      | **1b** (5/5) | **2b** (3/5) | **3a** (3/5) | **4a** (4/5) | **APPROVE w/ 7 constraints** |

---

## Minority Report (preserved for re-open)

- **The Simplifier** argued for `2a + 4b`: equities-only + assume-free metadata. **Deferred, not dismissed.** The council accepts the minimal-seam implementation style (reuse `fetch_definition_instruments` + `_upsert_definition_and_alias`), but rejects `4b` because registry writes sit on a live-path control plane. Frames coverage as `2b` because preserving futures support is part of the operator story post-PR.

- **The Scalability Hawk** argued for `3b` (bootstrap then IB enriches), plus mandatory retry/backoff, concurrency limits, advisory locking, and divergence metrics. **Safeguards adopted wholesale.** The formal `3b` label is overruled because the desired behavior is better expressed as `3a + explicit later IB qualification step`, not silent or automatic enrichment.

- **The Pragmatist** argued `2a` and raised a process objection: "don't burn a full 5-advisor council on this — it's a CLI branch extension reusing existing primitives, not net-new architecture." **2a overruled** for the same coverage reason as the Simplifier. **Process objection noted** — the chairman ruled the extra scrutiny was defensible given the registry's live-trading criticality, but flagged that this is heavier than ideal for single-user features in the future.

- **The Contrarian** raised the governing concern: "Databento has the symbol ≠ user can use the symbol." A registry row does NOT guarantee entitled historical bars for 1m/5m/1h backtesting, and does NOT guarantee IB can qualify/stream for live. **Concern adopted wholesale** — it is the main frame of the verdict. The council is not approving any design that lets operators infer research-ready or live-ready from Databento bootstrap alone.

- **The Maintainer** required a distinct verb (not `refresh` overload) + a crisp ambiguity/error contract before code is written. **Both adopted.** The council is not endorsing a fourth meaning of `refresh`.

---

## Missing Evidence (to resolve during Phase 2 research)

1. **Databento plan entitlements.** Does the current plan include `XNAS.ITCH` (NASDAQ), `XNYS.PILLAR` (NYSE), `ARCX.PILLAR` (NYSE Arca/ETFs)? 30-second check: `curl -H "Authorization: Bearer $DATABENTO_API_KEY" https://hist.databento.com/v0/metadata.list_datasets | jq`. If equity datasets aren't entitled, v1 is DOA and we pivot to `3b` (bootstrap via IB Gateway with a compose-profile toggle).

2. **Ambiguity behavior.** What does `fetch_definition_instruments` return for an ambiguous equity symbol (e.g., `BRK.B`, `BF.B`, dual-listed ADRs)? Needs a short spike to design the selection/retry contract.

3. **Rate limits and billing.** Real Databento metadata rate limits, billing behavior, and failure patterns for batch sizes the user actually runs (typical: 5-20 symbols per bootstrap, occasional 50+). Drives the `--max-concurrent` default.

4. **`BE-01` futures defect reproduction.** `CONTINUITY.md BE-01` reports `msai instruments refresh --provider databento --symbols ES.n.0` hangs with `FuturesContract.to_dict()` signature drift at `parser.py:188`. Needs reproduction on this branch to confirm the `2b` "futures-included" claim isn't vacuous.

5. **Advisory lock requirement.** Current database constraints — does the existing UniqueConstraint + CHECK already serialize the alias-upsert path, or is an explicit `pg_advisory_xact_lock` required? Spike: write two concurrent `bootstrap AAPL` calls and check for overlapping alias windows.

6. **Auto-heal completeness.** Is a Databento-seeded registry row actually sufficient to unlock the intended 1m/5m/1h backtest loop end-to-end, or is there hidden gating downstream (e.g., `verify_catalog_coverage` tolerance, Nautilus instrument pre-load from a different source)?

---

## Next Step

Per the chairman: draft the PRD (Phase 1 of `/new-feature`) with `AAPL`/`SPY`/`QQQ` as the spike symbols. Lock in these decisions in the PRD **before** any code is written:

- Command name: `msai instruments bootstrap` (new verb)
- `--provider databento` as required flag (pattern-match existing `refresh` CLI)
- `backtest-discoverable only` semantics in `--help` + API response + error messages
- Ambiguity contract: exception type + CLI/API message shape + retry guidance for multi-match symbols
- Rate-limit/retry/concurrency/metrics requirements from blocking constraint #1
- Advisory lock requirement from blocking constraint #2
- Divergence observability from blocking constraint #3
- End-to-end acceptance demo from blocking constraint #6 (AAPL + SPY + QQQ through the full pipeline)
- `BE-01` futures caveat from blocking constraint #2 — either fix it in this PR or explicitly call futures as existing-path-only

---

## What this means for the implementation

- **PRD:** `docs/prds/databento-registry-bootstrap.md` (Phase 1 output)
- **Research brief:** `docs/research/2026-04-23-databento-registry-bootstrap.md` (Phase 2, via `research-first` agent — resolves the Missing Evidence items above)
- **Implementation plan:** `docs/plans/2026-04-23-databento-registry-bootstrap.md` (Phase 3.2)
- **Branch:** already at `.worktrees/databento-registry-bootstrap` off `4bbdf46`

---

## Non-goals for this PR (deferred)

- **Options support** — distinct PRD required (chain loading, strike-band policy, OPRA entitlement). Nautilus gotcha #12.
- **Recurring catalog-sync cron** — would undo PR #37 operator-managed decision. Surface drift via divergence counter instead.
- **Databento-replaces-IB canonical** — prior council kept IB as the live-path authority. Re-open if Databento enrichment proves consistently more reliable after 3+ months of divergence telemetry.
- **`instrument_cache` → registry migration** — separate item in the deferred-items backlog (`Next` item #3 in CONTINUITY).
- **Symbol Onboarding UI/API** — user-facing surfaces for symbol declaration are a separate PRD (`Next` item #2 in CONTINUITY). This PR ships the CLI primitive only.
- **Bulk SP500 seed command** — if and when it becomes useful, v2 adds `--from-file sp500.txt` on top of the same primitive. Not needed for iteration-loop unblock.

---

## Follow-ups (not blocking this PR)

1. **After one clean month on Databento bootstrap** — does the divergence counter ever fire? If not, relax from "IB refresh required" to "IB refresh recommended" for Tier-1 equities.
2. **Architecture-governance review (2026-10-19)** — this decision feeds into the PR #36 postscript review: does the two-provider registry earn its complexity?
3. **Options PRD** — when a strategy actually needs them; not before.
