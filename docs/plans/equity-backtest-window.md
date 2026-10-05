# Inclusive equity backtest windows

Authoring status: diagnosis reproduced; independent reproduction REPRODUCED; first plan review found two material acceptance gaps addressed below. Production implementation had not begun when this revision was written. Current workflow progress and receipts live in task-local Forge state, not this plan's historical authoring status.

Immutable base: `main`, `b48d0ad88c4ebcff498d2c14dc6fb834bea9c32a`. Active branch/worktree: `fix/equity-backtest-window`, `.worktrees/equity-backtest-window`. This is the smallest next correctness slice of MASTER_PLAN Milestone1 and MASTER_MAP M15, not completion of the data/cost milestone.

## Problem and observed behavior

Public backtest and research requests use calendar dates. The runner documents their end date as inclusive, but forwards the date string unchanged to both Nautilus data filtering and engine execution. With pinned/installed NautilusTrader1.223.0, December3 means midnight at the beginning of December3.

Main's actual BacktestNode reproduction uses the current MSAI config builder, unchanged EMA strategy, a temporary five-bar catalog and fresh processes. Request December2–3 processes only the December2 bar. Control with end `2024-12-03T23:59:59.999999999Z` processes three bars including the final nanosecond of December3; the December4 midnight and afternoon bars are excluded. Separate actual runs over existing AAPL.NASDAQ catalog data process478 bars/December2 only versus980 bars/December2–3. This is execution evidence, not merely data availability or timestamp parsing. Task-local script, JSON and complete logs: `.forge/local/evidence/equity-backtest-window/reproduce_end_date.py` and `{synthetic,existing}-{primary,control}.{json,log}`.

The public BacktestRunRequest also accepts reversed dates; a December3–2 instance was accepted in the main's baseline schema probe. Research and portfolio schemas already reject reversal. No invalid job was submitted to reproduce this.

The actual API/queue/worker same-day December3 baseline `23b62f44-5c19-4aa7-bd2d-bb93268a8845` completed with0fills; the supported CLI and real browser showed the same empty result. A separate explicit-timestamp direct-engine reference over that day produced32fills and+$1.13 at1million opening capital with recorded commissions0.00USD. Its502 consumed bars follow from the actual980-bar two-day control minus478 December2 bars, and the repaired public jobs must directly report the native count, rather than infer it from fills. These are regression references, not validated trading costs or alpha.

Independent reproduction prerequisite is satisfied by dispatcher receipt `20261005T014632Z-37687-16484.receipt` with `reproduction_status=REPRODUCED`, using fresh Codex fallback after unavailable Claude reproduction capability. The earlier prose-only attempt is retained as UNVERIFIED (`20261005T014339Z-27430-25535.receipt`) and is not certification. The user explicitly approved this repair's bounded Anthropic Claude/OpenAI Codex review transport after automatic host rejection. Publication remains separate.

## Root cause and established contracts

- `backend/src/msai/schemas/backtest.py:22–23`, CLI backtest arguments and both browser forms use date-only inputs. SQL request/history dates remain those logical dates.
- `backend/src/msai/services/nautilus/backtest_runner.py:139–140` declares inclusive dates; `:448–461` supplies unchanged strings as both data and run cutoffs.
- Pinned Nautilus catalog and engine filter inclusively on `ts_init`; node intersects both config bounds. Both must receive the same normalized end. [Pinned catalog source](https://github.com/nautechsystems/nautilus_trader/blob/v1.223.0/nautilus_trader/persistence/catalog/parquet.py#L1962), [engine](https://github.com/nautechsystems/nautilus_trader/blob/v1.223.0/nautilus_trader/backtest/engine.pyx#L1482), [node](https://github.com/nautechsystems/nautilus_trader/blob/v1.223.0/nautilus_trader/backtest/node.py#L674).
- Current catalog coverage and portfolio return slicing already use midnight UTC through next midnight minus one nanosecond. The prior Databento ingest repair uses an exclusive provider boundary; its plain +1day cutoff would incorrectly admit the next midnight in Nautilus.
- Research train/test/holdout/purge/halving date arithmetic is already inclusive. This repair changes execution bounds once, not those splits or winner-selection rules.
- All production runner callers inspected pass date-only strings. Keep existing explicit timestamp inputs as exact instants if used internally; do not silently extend a timestamp to its whole day or introduce a new public timestamp API.

## Minimal change

1. Normalize the execution boundary once in the Nautilus runner: strict YYYY-MM-DD start at00:00:00 UTC and end at23:59:59.999999999 UTC. Set both BacktestDataConfig and BacktestRunConfig from those same values. Validate reversed calendar dates before expansion and normalized instant ordering afterwards; return clear ValueError for malformed or unrepresentable bounds. Use integer nanoseconds, including tests for pre1970, year9999 and the partial pandas maximum day2262-04-11. Do not turn end one day before start into an accepted equal-instant interval. Existing explicit timestamp inputs retain their exact instants; mixed date/timestamp input is ordered by its normalized bounds rather than rejecting a valid afternoon start with a same-day calendar end.
2. Add the equivalent existing end>=start Pydantic validation to BacktestRunRequest so reversed requests fail before persistence, resolution or queueing. Equality is valid. Add OpenAPI field descriptions explaining inclusive UTC calendar dates; preserve stored dates and existing response shapes. Research already has reversal validation; only its field descriptions may need the same clarification.
3. Add one concise explanation by the backtest and research date inputs: dates include both endpoint days in UTC. Update backtest CLI date help to match. In the backtest form, explicitly format Pydantic422 `detail` list messages using the existing research-form pattern, stripping the technical Value error prefix. Keep existing structured data-coverage errors usable. The authoritative server validator must yield correction guidance explaining end date must be on or after start date, not the current generic422 message.
4. Add optional `num_bars` to the existing numeric metrics dictionary, taken from the actual Nautilus result's `iterations`. Pinned engine source increments this once per consumed data item; the current runner loads only Bar data, so this is the total bar count across instruments, not unique minutes. Include it whenever a native result exists (including known zero), omit it when no native result exists rather than fabricate zero. Existing worker and research persistence already retain the metrics dictionary. Expose this same persisted count through API and CLI and show a concise Bars processed value in existing backtest results; legacy results with no count display Not recorded. No database migration, new metadata container, worker protocol, or logging framework is needed. The real-engine regression proves actual consumed timestamps and matching data/run cutoffs; public E2E asserts the persisted native count. Logs cannot substitute for user-interface assertions under Forge's testing policy.
5. Include the already checked release-status reconciliation in MASTER_MAP, MASTER_PLAN and docs/agent-context plus changelog. After acceptance, update M15 only with bounded evidence and link the solution. Old saved results remain historical; do not rewrite or recalculate them. Research and quick/full portfolio member backtests share this runner and can produce changed returns/rankings; their date splitting and slicing stay unchanged.

No dependency upgrade, new storage/service framework, ingest, credentials, broker orders or Azure change. Costs, exchange calendars, continuous futures, non-equity metadata, independent final holdout, allocation and live-capital readiness remain open. Newly corrected runs can legitimately have different fills, metrics and training rankings because they now include the missing day.

## Expected changed paths and ownership

Producer ownership, exact files only:

- `backend/src/msai/services/nautilus/backtest_runner.py`
- `backend/src/msai/schemas/backtest.py`
- `backend/src/msai/schemas/research.py` (field descriptions only)
- `backend/src/msai/cli.py` (backtest date help only)
- `frontend/src/components/backtests/run-form.tsx`
- `frontend/src/components/research/launch-form.tsx`
- `frontend/src/components/backtests/results-charts.tsx` (optional persisted bar count display)
- `frontend/src/app/backtests/[id]/page.tsx` (pass the optional count through)
- `frontend/src/lib/api.ts` (optional metrics key only)
- `backend/tests/unit/test_nautilus_backtest_runner.py`
- `backend/tests/unit/schemas/test_backtest_schemas.py`
- `backend/tests/unit/test_backtest_date_window.py` (small actual-engine regression using temporary catalog and subprocess isolation, no service/database dependency)

Coordinator ownership: this plan, solution documentation, the four carried assessment/changelog files, `tests/diagnostics/reproduce_backtest_end_date.py` (actual-engine reproduction program retained with the snapshot), graduated `tests/e2e/use-cases/backtests/inclusive-date-window.md` and its established tracked spec location after preliminary acceptance. Verifiers write task-local evidence only. All agents receive immutable base and exact scope; concurrent work must not revert others' changes.

## RED, GREEN and focused controls

Observe actual regression RED before production edits. The owning real-engine case must fail on missing final-day bars at this base. GREEN must demonstrate consumed timestamps, not only assert how config strings are formatted.

- Same-day request consumes its day's bars; multi-day consumes the final day's intraday and last-nanosecond bars.
- Exact next midnight is excluded from both query and execution; both bounds match in nanoseconds.
- The native numeric count equals the actual consumed Bar events in the real-engine fixture. A zero-bar native result is distinguished from a missing result/count; legacy rows are not changed.
- Adjacent one-day training/test executions do not overlap. Purge dates remain excluded, and logical persisted dates remain unchanged.
- Leap-day, month/year rollover and UTC behavior do not depend on the host timezone.
- Reversed date range fails before engine work and public request validation rejects it; valid equal dates succeed.
- Malformed/unrepresentable bounds such as year9999 fail clearly, without arithmetic overflow escaping as an opaque engine exception. Existing explicit timestamp semantics are preserved.
- Existing selection tests and portfolio inclusive-slice test remain controls. Broad backend regression belongs to CI, not repeated local full-suite runs.

Use primary's pinned Python environment against this worktree's module source until the guarded local runtime deliberately switches to the candidate. Run owning unit/real-engine checks, targeted research/portfolio boundary controls, Ruff/mypy for the change and frontend lint/build as appropriate. No new dependency installation needed.

## Real user journey acceptance

Actor: MarketSignal operator. Intent: request an inclusive calendar window and trust which days the strategy actually consumed. Interfaces: API first, actual supported Python CLI second, real browser through computer use third. Environment: existing guarded local research services and current AAPL.NASDAQ minute catalog. Broker/vendor guards remain active. Inventory all active jobs before any source-mount handoff; preserve PostgreSQL, Redis, data, images, volumes and unrelated projects.

1. API: submit a same-day AAPL December3,2024 EMA5/20,size1 backtest; await real queue/worker/Nautilus/persistence. Assert persisted `metrics.num_bars=502` from the actual native result,32fills and the same+$1.13 cash reconciliation as the direct-engine reference, with fill/series datesDecember3. Assert no December4 consumption in the synthetic owning regression; do not infer exclusion from missing real after-midnight trades alone.
2. CLI: submit the same window using the actual supported `python -m msai.cli backtest run` command, poll with `backtest show`, inspect trades/results. Assert its output includes native `num_bars=502` and the same logical dates and reconciled economics; compare deterministic results for equivalent configuration, not unrelated request IDs.
3. Browser: navigate to Backtests, submit the same-day window with the real form, follow the actual completed job, read the clarified date meaning, Bars processed502, native results and report, then reload and re-open through history. No mocked API or authentication bypass in the claimed journey. Capture reproducible text/screenshot evidence through available computer-use tools; do not invent saved image paths. Open the old reference and confirm its missing count is labelled Not recorded rather than zero.
4. Failure/correction: API and CLI reversed dates reject without a new persisted/queued job. Browser reversed date submission shows usable correction guidance; correct to December3–3 and finish on the same form. Record baseline and resulting job IDs.
5. Research: a small December2–5 sweep with holdout/purge and one walk-forward window keeps training choice separate from diagnostics and includes each window's endpoint day. Compare publicly returned trial `metrics.num_bars` against exact-window direct-engine controls, logical persisted boundaries and Discovery training provenance. Inspect through public research/trial API responses rather than logs or database files. Existing PR108 selection regression remains valid despite changed numerical results; no alpha claim.
6. Persistence/regression: saved100-fill legacy local reference remains byte-for-byte unchanged, reports/history keep requested dates, no vendor/broker work occurs. Existing portfolio slicing control passes; full shared-account portfolio acceptance is outside this slice.

Preliminary feature verifier may identify owning journey defects for bounded correction. Graduate proven cases and tracked specifications only after that acceptance. Freeze one staged-clean candidate, obtain distinct final spec/quality reviews plus verify-app and complete E2E receipts at that exact candidate. Publication needs an exact human decision; merge/deploy and subsequent Azure acceptance remain separate later steps.
