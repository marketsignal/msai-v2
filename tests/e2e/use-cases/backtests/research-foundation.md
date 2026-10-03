# Research foundation: backtest economics across API, CLI and UI

Surface coverage: **API Covered; CLI Covered; UI Covered**. These journeys cover the bounded realized-USD-balance repair. They do not establish Entra permissions, market-data completeness, realistic execution costs, research selection validity or broker trading. Run API → CLI → UI against one identified candidate.

Common setup: use the documented guarded local research stack, existing registered EMA strategy and AAPL minute data for January 22–24, 2025. Discover identifiers through supported APIs; never write directly to the database. Vendor ingestion/auto-heal and broker connectivity stay disabled. Use documented local development authentication and record the environment. The reference configuration is fast EMA 5, slow EMA 20, trade size `1`, instrument `AAPL.NASDAQ`, start `2025-01-22`, end `2025-01-25`. The January 25 bound is the documented workaround for the separately open inclusive-end-date defect. Do not reinterpret it as repaired date semantics.

## RF-API — Reconcile a strategy experiment

**Actor:** Research operator preparing to compare strategy versions.

**Scenario:** Existing data and strategy code are available; the operator needs a reproducible account-level result before assessing the idea.

**Interface:** API.

**Intent:** Run a reference experiment and understand its measured economics and executions.

**Setup:** Common setup, healthy candidate API and idle research capacity. Discover the strategy and existing data through supported reads.

**Steps:**

1. Submit a new reference backtest and poll its public status until terminal.
2. Retrieve results, all execution pages (page size 30 exercises pagination), and the HTML report.
3. Independently sum signed fill cash flows and recorded fees using decimal arithmetic. Confirm that the reference ends flat before equating that sum to balance change.
4. Compare opening/ending balance, first-session change, daily compounding, monthly return and headline/report figures. Re-request the same run and history.

**Verification:** The client receives a completed run with version-1 accounting, $1,000,000 initial balance, 100 unique fills, 50 round trips, $0 recorded fees, and $999,999.47 ending balance. Recorded daily changes are −$0.04, +$0.38, −$0.87; total ratio is approximately `-5.3e-7` (floating reconciliation tolerance `1e-12`). Reported total return is `−0.0000530%` and drawdown `−0.0000870%`. Unknown per-fill P&L is null, not zero. The report identifies realized-balance, cost and sample limitations. Legacy reads have absent accounting and unknown fill economics rather than newly certified zeros.

**Persistence:** Follow-up requests return the same ID, execution identities and economics. Historical legacy storage is not rewritten.

## RF-CLI — Run and retrieve research from a fresh process

**Actor:** Strategy author working from the repository root or backend directory.

**Scenario:** Shared Compose configuration used to prevent CLI startup; the author wants to submit and revisit experiments without working around import failures.

**Interface:** CLI.

**Intent:** Reliably operate and export the same reference experiment through documented commands.

**Setup:** Common setup. Select the candidate source/interpreter and set the documented API URL/key in the process environment. Use synthetic invalid settings for diagnostics; never publish real dotenv values.

**Steps:**

1. Invoke help from the root setup, then submit the reference with `backtest run` and its configuration JSON.
2. Retrieve status/results through a fresh `backtest show` invocation; export every page through `backtest trades --all` and retrieve the report.
3. Reconcile the result as in RF-API and compare the export's accounting, null P&L and actual zero recorded fees. Export a legacy run and distinguish its null accounting/economics.
4. Invoke help with a synthetic invalid integer setting and inspect the diagnostic.

**Verification:** Stdout shows the new run and completed results with the same financial meaning as RF-API. Full-page JSON exports retain accounting metadata. Invalid configuration explains the affected field without echoing the sentinel input; it cannot report success. A focused CLI regression additionally proves inconsistent page metadata refuses export without overwriting an existing file.

**Persistence:** A new CLI process retrieves the same run and report. Compare returned IDs and economics, not only exit codes.

## RF-UI — Submit, interpret and revisit a backtest

**Actor:** Research operator using the dashboard with documented local development authentication.

**Scenario:** The operator needs to evaluate an EMA version and distinguish executions, closed outcomes, unknowns and unrealized performance.

**Interface:** UI through real browser computer use, actual backend/data, no mocked API responses.

**Intent:** Run an experiment, understand the result and recover from a failed attempt.

**Setup:** Common setup and already-serving candidate frontend. Set browser viewport to at least the supported desktop reference of 1280×720. Local authentication is not Entra acceptance.

**Steps:**

1. Open Backtests and submit a reversed date range. Observe terminal failure and its date explanation; return to history. This currently fails late in the engine and is recorded as remaining usability friction.
2. Submit the valid reference. Use **Refresh history** to observe current status, then open its detail page.
3. Inspect capital/scope text, native metrics, first-session chart tooltip, monthly tooltip, all fill rows and Full report. Read complete signs, digits and percentage suffixes without clipping.
4. Open a legacy run and observe its legacy labels and unavailable P&L/fees. Return to the new run, reload, and confirm persistence.
5. Switch history to Single and refresh; only that selected scope may appear. If a genuine load failure occurs, the page must show unavailable history and Retry, without claiming empty history or retaining another filter's rows. Retry must retain the selected filter. Do not inject mocked responses into this real journey.
6. On an execution-log loading/error state, confirm it does not claim zero records or an invented page count. If an error occurs, use its Retry action and confirm the same run/page recovers with the correct fill or legacy interpretation.

**Verification:** The operator sees the same 100 fills, balance-return/drawdown values and recorded-cost limitations as the API reference. Per-fill P&L is visibly Unavailable; new recorded zero fees differ from unknown legacy fees. The report can open and its primary metrics agree. Failure is distinguishable from completion and a valid action works afterward. Rounded monthly cells explain that their tooltip supplies full precision. History offers explicit refresh, ignores superseded responses and does not equate failed reads with no experiments.

**Persistence:** Revisiting/reloading shows the same run, correct result and fills. Repeated first-load transport failures remain an explicit unresolved reliability finding unless separately diagnosed; successful reload alone does not certify a transport repair.

## Supporting regression coverage

`frontend/tests/e2e/specs/backtest-history-state.spec.ts` uses isolated mocked responses to reproduce history errors, stale filter races and pending→completed refresh. `backtest-readability.spec.ts` checks actual rendered text/axis geometry at desktop widths. `backtest-accounting.spec.ts` checks pure chart/percentage functions. These checks support the real journey and do not replace it.

The default Playwright configuration must actually launch a fresh frontend when no target is supplied; an explicit `PLAYWRIGHT_BASE_URL` leaves the existing environment's server and authentication management alone. Record the executed commands and distinguish a missing browser/runtime from a product failure.
