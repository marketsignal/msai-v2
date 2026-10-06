# Native Nautilus V2 research integration

The bounded V2 compatibility trial reproduced application import, configuration, catalog and
execution gaps. This candidate integrates exact Nautilus `2.0.0rc6` into one isolated research
application so an existing EMA strategy can be discovered, validated, executed and revisited
through the API, installed CLI and browser. The accepted V1 release and saved experiments remain
the reference. This candidate has not been installed in production.

## Native boundary and execution

The application retains FastAPI, arq, the spawned common backtest runner, native BacktestNode,
PostgreSQL and existing report delivery. Explicit native StrategyConfig constructors declare the
same user fields/defaults. One small Pydantic boundary derives discovery schemas and validates
those constructor annotations, converting native instrument/bar IDs and Decimal quantities.
Native RC6 construction alone accepts unknown kwargs, so the application rejects missing,
unknown and invalid user fields before dispatch. Instrument/bar identifiers retain the existing
engine injection rules. There is no second V1 configuration path.
The runner forwards the validated native field values when reconstructing the strategy.
For example, a validated boolean stays a boolean; native identities and Decimal quantities
retain their string representations without changing the caller's saved input.

RC6 configures exception propagation on `BacktestRunConfig(raise_exception=True)`;
its native `BacktestNode.run()` takes no keyword argument. The callback amendment
corrects the original plan's older method-call spelling. The retained live risk mixin
also has a V1 `modify_order(order, ...)` contract, while RC6 expects a client order ID.
The four example classes document that precise type boundary: they do not modify orders
in research, their mixins remain unarmed, and RC6 live operations refuse explicitly.
This does not certify that retained live modification contract on V2.

The catalog adapter supplies supported Equity metadata and Decimal128/UTC nanosecond input to
native BarDataWrangler and typed catalog operations. Unsupported asset/interval requests refuse.
Candidate catalog files use a separate V2 namespace; the original catalog is retained for recovery.

Actual callbacks exposed a removed EMA `Portfolio.is_flat` method after discovery checks passed.
RC6 provides `Portfolio.is_net_flat`, `is_net_long` and `is_net_short`; the repair changes only the
missing flat check. A five-bar actual native experiment failed with zero fills before the repair
and executes two fills after it. The native EMA indicator remains unchanged, including RC6's
mean-seeded warm-up. Discovery and construction checks alone do not certify callback execution.
Native actors can log Python callback exceptions and continue; the runner also refuses success
when its actual strategy callbacks fail rather than persisting a completed zero-result experiment.

The corrected representative native trace records all 390 callbacks and 16 fills. Bar timestamps,
closes, indicator counts/initialization flags, decision timestamps and fill economics match V1 on
this fixture. Warm-up EMA values differ because RC6 seeds at the arithmetic mean after its period
samples. That observed difference is retained; matching +$2.10 is not forced indicator parity.
Native venue/trade ID formats differ and their actual identities remain in stored reports.

## Result meaning

New accounting JSON optionally records engine version, capital/leverage, fee model, commission
per fill, fill model/seed, configured random slippage probability, execution assumptions and
synthetic fixture identity/manifest SHA256. Existing results keep their original bytes and
accounting basis. Missing settings mean **Not recorded**, never assumed zero.

The detail page labels this **Research simulation** and retains realized-account-balance meaning:
open-position unrealized P&L is excluded. Return values remain ratios in the API and are converted
to percentages for display. Fill records represent executions. New reports include the recorded
assumptions; the historical report's `save()` handler is retained with its original bytes.

Default runs use USD, leverage 1, FixedFeeModel with zero modeled commission and DefaultFillModel
with seed 42 and zero configured random slippage. These configured values do not establish
realistic zero execution costs. For L1 LAST/MID bar execution, native quotes share the trade price
and OHLC legs receive quarter-volume liquidity. Exhausting displayed L1 liquidity can worsen a
residual fill by one tick independently of random slippage. The prior ProbabilisticFillModel trial
also widened its synthetic book independently of a zero configured probability; selecting the
native DefaultFillModel makes this candidate's tiny reference deterministic without replacing
native execution mechanics.

The bounded independently calculated control buys at $100 and sells at $103 with $1 per fill:
ten shares produce $30 gross less $2 commission, or $28. That single-instrument reference does
not close the retained shared-cash, aggregate-margin or tick trial failures, full NAV, realistic
costs or portfolio/account/risk acceptance.

## Scope and observed evidence

The [runbook](../../runbooks/nautilus-v2-research.md) owns sanctioned setup and recovery. Its
deterministic AAPL fixture contains synthetic minute bars on January 2025 UTC weekdays, including
holidays. It is not an exchange-session calendar, evidence of complete data or alpha validation.
The registry carrier is Databento metadata; no vendor download or live qualification is implied.

Baseline API/CLI/browser readback and report hashes were captured before dependency changes.
Isolated Linux RC6 startup and focused native config/callback/catalog/economic checks have run.
The initial failed public EMA candidate is retained as `FAIL_BUG`. Repaired API and independently
submitted installed CLI experiments each recorded 390 bars, 16 fills and +$2.10 on $1,000,000 USD,
matching the V1 reference economics. Public fill cash flows independently reconcile the API run;
new accounting records the actual RC6 settings and synthetic manifest. CLI date correction,
persisted show and report delivery were captured. Actual browser negative-size validation and
correction, single-date submission, matching native metrics/fill rows, full report and reload/history
reopening passed; saved baseline assumptions still display **Not recorded**. New report rendering
had no new console error. The first research sweep's missing engine identity failure remains
retained as `FAIL_BUG`; the common runner now prepares instrument/bar identity before shared
validation for research callers too. The repaired two-choice sweep selects training index 0,
with holdout/full replay kept as diagnostics. Two chronological walk-forward windows select
latest index 1 despite a larger earlier-window objective. API and repeated CLI research reads
agree; actual browser exploratory Discovery creation/refusal and provenance reload/reopen passed.
Failed research promotion refuses 409 without creating a candidate. Compatible recovery passed
against the same isolated database: immutable V1/1.223 read the original saved result/report and
reran 390 bars, 16 fills and +$2.10 through public interfaces/installed CLI. Restoring RC6 preserved
the accepted candidate payloads/fills/reports and rechecked startup/refusal. No dump restore or
saved-record manipulation was used. Final candidate certification remains pending.
Preliminary acceptance passed through fresh public API/CLI/research/
recovery verification and a separate fresh actual-browser closure. The initial PARTIAL verifier
report remains retained with its browser-tooling limitation; root observations are also attributed
separately. The [graduated journeys](../../../tests/e2e/use-cases/backtests/nautilus-v2-research.md)
and opt-in real-service browser regression record the accepted bounded workflow.
No local auth bypass certifies Entra. Live start/resume, incompatible cold reads and release
readiness refuse explicitly on this research runtime; generic liveness is not live readiness.
Immutable experiments, independent validation, complete sessions and realistic costs remain open.
