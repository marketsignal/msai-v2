# Research-foundation candidate verification

Date: October 3, 2026. Candidate: `fix/research-foundation`, based on `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`, in the isolated managed worktree. This is local research proof; Azure still runs its previously observed source and has not received this repair.

Application source manifest: `46fdda3fc3702b97b0dd76bcb4398ff5839ed11072e8ed98d96a093b7ab6fa16`, SHA-256 of sorted JSON mapping the 13 changed application source files plus Playwright configuration to content hashes. This tracked account records evidence before candidate freeze. Final Forge candidate identity, read-only review and verifier receipts are separate local evidence; no commit or release is implied by this report.

## Automated and startup evidence

Producer tasks observed failing regressions before repair and then passed 103 accounting/worker/control tests, 47 schema/API tests, 35 CLI/configuration tests, 11 CLI export tests, four chart/precision tests, four history browser tests, five readability/execution-log browser tests and four harness-configuration tests. Focused lint/types passed. The integrated Next.js 15.5.12 production build passed before the later UI follow-ups; the final verifier must build those final bytes. Existing unrelated full-suite findings are not closed by these focused checks.

The original forwarded server command failed before startup. The corrected command started a fresh host server in 2.8 seconds; a real browser rendered `/backtests`. The actual Playwright-managed fresh-start control also passed: with only the Docker frontend stopped, no explicit target and `CI=1`, the harness launched its own server and ran all four chart checks in 15.4 seconds, then cleaned up. The same Docker frontend was restored and `/backtests` returned 200. First route compilation can take tens of seconds. Local development authentication does not prove Entra or partner authorization.

## Real API reference

The independent feature verifier submitted run `0ae4db4e-8ac1-4843-b7e0-e5a99fec6b49` through the candidate API using registered `example.ema_cross`, existing AAPL minute bars for January 22–24, 2025, fast EMA 5, slow EMA 20 and quantity 1. It retrieved all four pages of 30 fills and independently reconciled decimal cash flows. The January 25 end bound preserves the existing explicit workaround; the broader inclusive-date finding remains open.

| Measurement | Reconciled result |
| --- | ---: |
| Initial / ending balance | $1,000,000 / $999,999.47 |
| Net change with engine-recorded fees | −$0.53 |
| Actual fills / completed round trips | 100 / 50 |
| January 22 / 23 / 24 change | −$0.04 / +$0.38 / −$0.87 |
| Return ratio / displayed percentage | −0.00000053 / −0.0000530% |
| Recorded commission | $0; realistic broker costs unverified |
| Per-fill realized P&L | Unavailable; no placeholder zero |

Daily compounding, monthly return and headline return agreed within `1e-15`; report drawdown displayed `−0.0000870%`. Results, fills and history were stable on re-request. Legacy run `7e10e3c7-74ec-4e99-96df-817ecda44653` still has 100 historical records, but absent accounting metadata and null P&L/commission identify them as unverified legacy order records. No historical rows or reports were rewritten.

## Real CLI and browser evidence

The real CLI submitted run `c7412adc-0bf7-4f22-b052-184db1801d80`, which completed with the same 100 fills and reconciled loss. Fresh CLI invocations retrieved results, all fill pages and the HTML report. A synthetic invalid configuration failed with a field explanation and without its sentinel input value. The verifier found that `trades --all` discarded accounting metadata; a bounded repair passed 11 focused tests and the real CLI re-check now retains version 1 for new results and explicit null for legacy exports, preserving null/zero economics. Conflicting metadata between pages refuses export without overwriting an existing output file.

Real-browser submission `c4cd3076-20bb-4bac-8ab0-f8433e0b76c8` completed with the same economics. The verifier independently summed the 100 visible fill rows to −53 cents, observed unavailable per-fill P&L and recorded $0 fees, and opened the full report. The first-session tooltip showed `−0.00000400%`; the monthly cell's full-precision tooltip showed `−0.0000530%`. Legacy reload displayed 100 explicitly unverified records with both P&L and fees unavailable.

The negative reversed-date run `78ac6510-8fbd-49d0-97be-533e4ece8d64` failed with the offending dates in its error, and the subsequent valid action succeeded. Validation is late and technical (`ENGINE_CRASH`); this remains part of date/input usability work rather than a claim that invalid ranges are rejected before queuing.

The real browser exposed false-empty/stale-filter history states and clipped tiny percentage values. Four isolated browser regressions failed before the history repair and passed afterward. A subsequent genuine Single-history fetch failure showed only the error and Retry; clicking Retry recovered 20 Single rows and Refresh preserved that scope. Real browser reinspection confirmed complete card values and chart-axis labels at 1280×720; isolated geometry checks cover 1024, 1280 and 1440 widths.

The preliminary verifier returned **FAIL** because the execution log still claimed zero records alongside a failed fetch. The subsequent repair distinguishes loading/unavailable counts and adds explicit same-page Retry; its versioned/legacy controls passed after RED. Real closure and final candidate acceptance are recorded separately and must not relabel the preliminary failure. Intermittent initial IAB fetch failures recovered on Retry/reload. A bounded separate Chromium comparison did not reproduce them: successful history controls showed 20 rows and eight legacy execution requests returned 200 with 100 rows; one cold-route timeout and one diagnostic-induced navigation abort were excluded from product findings. This comparison suggests a browser/runtime difference but does not establish the cause. No transport repair is claimed.

The full report also logged an undefined `save` function while rendering its content successfully; no user-visible accounting failure was demonstrated. Screenshot evidence is retained with local verifier reports. Final frozen-candidate reviews and verification determine acceptance beyond this pre-freeze account.

## Operating and research boundary

Seven local research services use candidate worktree source and the existing database/data volumes. Inspected runtime guards disable vendor ingestion/auto-heal and broker connectivity. No migration, Azure mutation, gateway/live deployment or broker order was performed. Three May portfolio records remain marked running without matching queued jobs; this stale-status problem remains part of operational work.

Realized account balances exclude unrealized open-position gains/losses. Costs are engine-recorded, not a calibrated IB fee/slippage model. Exchange-calendar sampling, complete marked-to-market NAV, data identity/completeness, interval/end-date semantics, futures, independent holdout/selection and portfolio/account execution remain open Master Plan gates. A three-session reference validates wiring and measured accounting, not alpha or production readiness. Intermittent browser history failures observed before this journey remain unresolved unless later evidence establishes their cause and repair.
