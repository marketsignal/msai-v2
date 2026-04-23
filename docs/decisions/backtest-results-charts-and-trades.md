# Decision: Backtest Results — Charts & Trade Log

**Status:** RATIFIED 2026-04-21
**Feature branch:** `feat/backtest-results-charts-and-trades` off `main@cc7d213`
**Decided via:** Standalone `/council` (5 advisors + Codex chairman)
**Participants:** Pablo (user), Claude (orchestrator), Simplifier / Scalability Hawk / Pragmatist (Claude advisors), Contrarian / Maintainer (Codex advisors), Codex xhigh chairman

---

## The Question

Pablo (verbatim): "I want to be able to see, for every backtest that ends, the whole tear sheet with all the risk metrics and all the charts. For example, Quantopian gives you this huge report, Pyfolio, which is my favorite. It tells you all the risk metrics, all the charts, and all time series. You can see Sharpe stats or Sortino ratio, and you can see them month by month, yearly. For every backtest that it finishes, I should be able to see all that and also download it if I need to download it."

How do we get a Pyfolio-style tear sheet for every backtest into the React UI, with download option, shipped as one PR on this codebase?

## The Architectural Grid

|                     | Option 1                               | Option 2                                | Option 3                                                           |
| ------------------- | -------------------------------------- | --------------------------------------- | ------------------------------------------------------------------ |
| **(A) Rendering**   | A.1 Iframe QuantStats HTML             | A.2 Native React Pyfolio port (2–3 wk)  | A.3 Hybrid: native for 4 MVP components + iframe for "full report" |
| **(B) Persistence** | B.1 Worker computes timeseries → JSONB | B.2 Parse HTML on GET                   | B.3 Persist raw account_df pickle + on-demand compute              |
| **(C) MVP scope**   | C.1 4 components only                  | C.2 4 components + full-report fallback | C.3 Full native Pyfolio equivalent                                 |

## The Verdict

**Chosen: `A.3 + B.1 + C.2`** with modifications to B.1 per the Contrarian's drift objection and the Maintainer's atomic-transaction gate.

### What ships in this PR

1. **Worker writes one canonical `Backtest.series` JSONB payload** in `_finalize_backtest()`, atomically with `metrics`, `report_path`, and `Trade` rows. Contents = daily-compounded normalized returns/equity series derived from the same normalization path that feeds QuantStats (dedupe `build_series_from_returns` vs `_normalize_report_returns` into one path). All chart views (equity curve, drawdown, monthly returns, yearly breakdown) derive from that single source.
2. **`series_status: Literal["ready", "not_materialized", "failed"]`** persisted alongside to disambiguate old backtests (NULL series) from compute failures.
3. **`GET /api/v1/backtests/{id}/results`** extended to return `metrics`, `series_status`, `series` (when ready), and a bounded/non-paginated aggregates payload.
4. **NEW `GET /api/v1/backtests/{id}/trades?page=N&page_size=100`** paginated sibling endpoint. `/results` stops returning trades inline.
5. **4 native React components wired up** (Equity Curve, Drawdown, Monthly Returns Heatmap, Trade Log) — same scaffold that's already in place, just with real data piped through.
6. **"Open Full Report" tab/button** embeds the QuantStats HTML via an **iframe sourced from a Next.js server-side proxy route** (`/api/backtests/[id]/report` inside the Next.js app), which attaches the user's auth header before streaming the backend's `/report` FileResponse. Chairman rejected token-in-query new-tab shortcuts (leaks credentials to browser history).
7. **Observability:** `msai_backtest_results_payload_bytes` histogram + WARN log when `series` JSONB payload exceeds 1 MB. `msai_backtest_trades_page_count` counter on the new `/trades` endpoint.
8. **Frontend:** `BacktestTradeItem` TS type matches individual-fill shape (backend sends `{id, instrument, side, quantity, price, pnl, commission, executed_at}`). TradeLog columns adjusted accordingly — no entry/exit pairing this PR.

### Must-do constraints (blocking objections from council, all honored)

| #   | Constraint                                                                                                            | Source           |
| --- | --------------------------------------------------------------------------------------------------------------------- | ---------------- |
| 1   | Authenticated iframe proxy (no token-in-query)                                                                        | Contrarian       |
| 2   | `series` payload persists canonical daily-normalized returns, not pre-digested chart blobs                            | Contrarian       |
| 3   | One atomic write in `_finalize_backtest()` transaction                                                                | Maintainer       |
| 4   | Storage name `series` (matches `PortfolioRun.series` precedent) — NOT `timeseries`, `equity_curve`, `returns_payload` | Maintainer       |
| 5   | Single returns-normalization code path (dedupe the two)                                                               | Maintainer       |
| 6   | Explicit `series_status` availability/error field                                                                     | Maintainer       |
| 7   | Paginated `/backtests/{id}/trades` sibling endpoint                                                                   | Scalability Hawk |
| 8   | Daily-compound/downsample at worker-write time; never ship raw per-bar to JSONB or browser                            | Scalability Hawk |
| 9   | Payload-size observability (histogram + WARN log)                                                                     | Scalability Hawk |
| 10  | No `B.2` (parse HTML on GET) — unanimously rejected                                                                   | All              |
| 11  | No `C.3` (full native Pyfolio port) — out of scope for v1                                                             | All              |

### Parity contract between artifacts

| Artifact                                      | Source of truth for                                         | Notes                                       |
| --------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------- |
| `Backtest.metrics` (JSONB)                    | Aggregate scalars (Sharpe, Sortino, etc.)                   | Wins for summary numbers                    |
| `Backtest.series` (JSONB)                     | Daily equity curve, drawdown, monthly returns               | Wins for native chart views                 |
| `trades` table                                | Individual Nautilus fills                                   | Wins for trade log (via paginated endpoint) |
| `Backtest.report_path` → QuantStats HTML file | Downloadable presentation artifact (snapshot at write time) | NEVER wins over DB; iframe is viewer only   |

If `series` and QS HTML diverge (e.g., future QS version uses different compounding), `series` wins for API consumers; HTML is flagged stale-but-viewable in the UI.

### Sharing / outside-viewer stance (Pablo 2026-04-21)

Pablo may share a backtest with an outside party. **Share pattern for v1 = download the QuantStats HTML file and send it**, not a live URL to the detail page. So the iframe inside the detail page remains Pablo-only (authenticated), and the desktop-only QS HTML aesthetic is an acceptable cost because outside viewers see the file, not the React UI.

If later Pablo wants a live-link share with public access (investor view), that's a follow-up PR with its own auth story (signed URL / anonymous token / public-backtest flag).

## Minority Report (preserved per council protocol)

**The Contrarian (Codex) objected** with OBJECT verdict. Their three concerns:

1. **Iframe auth flaw** — fatal assumption that iframe is "basically free". `/report` is header-auth-only; iframe `src` can't attach headers. **UPHELD.** The Next.js proxy route is now a mandatory deliverable in this PR, not a shortcut.
2. **B.1 creates second analytics truth that drifts** — storing pre-digested chart blobs means the first new chart request (benchmark overlay, rolling Sharpe, etc.) forces more one-off blobs or recompute. **SUBSTANTIALLY UPHELD.** `B.1` redefined: persist canonical normalized daily series, derive all views from that. Future charts become view-layer transforms of the same `series` payload, not new JSONB blobs.
3. **A.2 (native-only, no iframe) was dismissed too fast** — the hard part is preserving canonical series, not reimplementing charts. **OVERRULED for this PR** because Pablo explicitly said "I want to see all that now." Proxied QuantStats delivers immediately. Native slice expands incrementally from the persisted series in future PRs. If the iframe-UX cost (mobile, print, outside-viewer) becomes real later, the persisted `series` means we can build native-only without re-architecting storage.

**The Maintainer's (Codex) conditional gates** are accepted as blockers, not deferred. Specifically: atomic write in `_finalize_backtest()`; `series` storage name; explicit parity/precedence contract (documented in this file, table above); availability/error signal via `series_status`.

## Consensus Points (all 5 advisors agreed)

- No advisor supported `B.2` (parse HTML on GET) or `C.3` (full native Pyfolio port).
- The current behavior of losing `account_df` after the worker exits must end.
- MVP includes the native slice now, using the 4 already-scaffolded components.
- QuantStats HTML remains available in MVP as a supplemental artifact alongside the native UI.

## Missing Evidence (accepted open risks for this PR)

- QuantStats HTML structure stability across minor versions — needs a 2-3-version sanity test.
- Whether 3–5 MB QuantStats HTML renders cleanly in target browser iframe — needs ~30-min spike.
- Wall-clock + payload-size cost of `series` build on a 5-year minute-bar `account_df` — needs benchmark during TDD.

All three will be addressed during Phase 2 research or Phase 4 TDD; none block this decision.

## Answers to the 10 implementation questions

(From the original PRD discussion round 1.)

| Q                     | Answer                                                                                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Q1 trade pairing      | Individual fills (defer round-trip pairing)                                                                                                            |
| Q2 timeseries source  | Worker computes canonical daily-normalized series; persists to `Backtest.series` JSONB atomically; dedupe against existing `_normalize_report_returns` |
| Q3 monthly heatmap    | Derive from same canonical series in worker                                                                                                            |
| Q4 endpoint shape     | Split: `/results` keeps aggregates + `series`; paginated `/backtests/{id}/trades?page=N` is new                                                        |
| Q5 granularity        | Daily always; compound at worker-write time                                                                                                            |
| Q6 benchmark overlay  | Deferred to a future PR (not blocking)                                                                                                                 |
| Q7 MVP trim           | 4 components + "Open Full Report" iframe tab. Cut line: drop monthly heatmap first                                                                     |
| Q8 trade columns      | Individual-fill shape: Timestamp, Instrument, Side, Qty, Price, P&L, Commission                                                                        |
| Q9 persistence column | ONE JSONB column `series` (matches `PortfolioRun.series`)                                                                                              |
| Q10 backward compat   | All new fields nullable; `series_status` disambiguates old rows from compute failures                                                                  |

## Deferred / out of scope (explicit non-goals for this PR)

- Full native Pyfolio port (60+ stats as React components) — future PR if iframe UX becomes unacceptable.
- Benchmark overlay (SPY/QQQ) — future PR.
- Entry/exit round-trip pairing in trade log — future PR.
- CSV export of trades — future PR.
- Public-shareable link / signed URL for detail page — future PR with its own auth story.
- Rolling metrics (3m/6m/12m Sharpe, etc.) beyond what the `series` payload enables — future PR.

## Next Step

In `backend/src/msai/workers/backtest_job.py`, add a single `_finalize_backtest()` write path that produces the canonical normalized-daily `series` payload and commits it atomically with `metrics`, `report_path`, and terminal status. This is the architectural keystone; all frontend embedding work depends on it.
