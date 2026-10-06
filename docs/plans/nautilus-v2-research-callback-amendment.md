# Bounded implementation amendment: native callback failures

The first actual Linux API experiment returned completed/390 bars/zero fills,
while actual native instrumentation reproduced a removed `Portfolio.is_flat`
call in the tracked EMA callback. This is a
T2 execution defect and a T4 success-reporting defect, not an accepted indicator
or profit difference. The original plan's discovery checks alone did not expose it.

T2 repairs that call using native `Portfolio.is_net_flat`; exact RC6 retains
`Portfolio.is_net_long` and `is_net_short`. An actual RC6 callback regression
establishes the contract. It preserves the native indicator, signal
semantics, order size and existing backtest risk boundary.

Pinned RC6 `crates/common/src/actor/data_actor.rs::handle_bar` logs ordinary
callback errors without latching the engine's callback failure condition.
`BacktestRunConfig(raise_exception=True)` therefore cannot by itself guarantee
Python strategy callback success. No native StrategyConfig/ActorConfig callback
propagation setting exists; logger shutdown cannot certify complete execution.

RC6's native `BacktestNode.run()` takes no exception keyword. The original plan's
`run(raise_exception=True)` spelling is corrected here: configure the run with
`raise_exception=True`, then call `node.run()` without arguments. The pinned binding
is `crates/backtest/src/python/node.rs` at revision
`7b766f8825b2539c5b2ac1375e9d97b41c509edb`; the runner uses that native contract.

T4 adds a subprocess-local context in its owned `backtest_runner.py` around the
actual Python strategy callback overrides. It records the first exception and
traceback, rethrows the original exception unchanged to the native actor, and
refuses success before extracting/persisting results after the native run. It
restores original methods in `finally`. Actual native failing start/bar callbacks
establish RED/GREEN, with successful native references and ordinary execution as
direct regression checks. There is no logging-text parser, native engine fork,
replacement indicator/fill/accounting ledger, or rewrite of saved experiments.

Public job `c63768c7-ba3e-4a84-9251-3c4fb053a347` and its captured observations
remain preserved as FAIL_BUG. Repaired image execution, actual EMA behavior trace,
and the remaining API/CLI/browser/research/recovery journeys must pass before
final freeze. Final same-candidate reviews cover this amendment and implementation.
