# Nautilus version decision: executable compatibility trial

**October 5, 2026. MSAI source baseline: `84707a47313766ef3fbe5c251f8488b9dc2cb822`.** This follows the [source/package assessment](2026-10-05-nautilus-native-upgrade.md). Three specialist agents investigated research economics, live lifecycle/state and exact upstream contracts; the coordinator resolved the complete backend dependencies, tested Linux packaging and checked the decision against the observed failures. [Pinned upstream facts and dependency details](2026-10-05-nautilus-upstream-trial-addendum.md).

## Decision

**Target a direct V2 migration in development; do not schedule a 1.231 production bridge by default. Keep the running 1.223 reference until the replacement passes application and release acceptance.** RC6 is the evaluated target, not a deployment recommendation. Refresh the exact release before implementation and again before promotion.

V2 executed the basic research economics and useful native risk controls, while a native-only live lifecycle can retain Redis persistence. The 1.231 bridge preserves familiar APIs but did not resolve the exercised funding/risk boundaries and introduces a separate IB Async downgrade. Shipping it first would add a dependency acceptance cycle without a demonstrated benefit to the next milestone. This is a development sequencing decision, not a claim that V2 is complete or production-ready.

The immediate implementation should deliver **one real MSAI research journey on V2**, plus the minimum application/import isolation needed to keep the existing live interface coherent. Do not build a general two-engine framework. Before committing to broader live/portfolio migration, require a small supported lifecycle/event/cache adapter and a resolution or explicit restriction for the simultaneous-funding case. Reconsider a 1.231 bridge only if a specific verified fix gets the operator a needed capability sooner, V2 requires substantial replacement infrastructure, or live operation becomes urgent before a suitable V2 release is accepted. Upstream explicitly discourages RCs for production/live real capital; the trial does not override that guidance.

## Scope and controls

- Exact candidates: **1.231.0** and **2.0.0rc6**, compared with installed **1.223.0**. Both candidate backend locks were resolved in private copies. No tracked application dependency, source, strategy or production dataset was edited.
- Native research fixtures use fresh temporary catalogs, synthetic flat OHLC bars, explicit capital, leverage, fees and fill settings. They exercise real `BacktestNode` execution and native order/fill/position/account reports. They are deterministic contract examples, not market-realistic strategy backtests or alpha evidence.
- The equity instrument is AAPL; the futures instrument is ESZ4 with a $50/point multiplier, 0.25 tick and December 20, 2024 expiry. The modeled 5% futures margin is an explicit fixture assumption, not a verified historical exchange margin schedule.
- Native live checks use simulated execution and a separate temporary Redis instance on loopback port 16391. No broker, vendor feed, account credential, existing Redis database or application service is used by those fixtures.
- Linux image checks use the tracked Dockerfile and a secret-free copy of tracked backend/strategy files, build locally for `linux/amd64`, and do not publish or deploy images. Network-disabled import checks are distinct from application startup and browser acceptance.
- Executable scripts, exact commands, dependency diffs and JSON/log evidence live under the private `.forge/local/evidence/nautilus-trial-2026-10-05/` directory. Failures are retained alongside positive controls. This investigation does not reuse the earlier documentation quick-fix's verification receipts.

## Research behavior actually executed

| Contract | 1.223.0 | 1.231.0 | 2.0.0rc6 | Meaning |
| --- | --- | --- | --- | --- |
| Equity catalog → node → reports | Pass | Pass | Pass | Buy 10 at $100, sell at $103, $2 total fees: **$28 net**, ending capital $10,028, **0.28% account return**. |
| Futures metadata and economics | Pass | Pass | Pass | One ES contract moves one point: $50 gross minus $2 fees = **$48 net**, ending capital $100,048. |
| Inclusive execution bounds | Pass in fixture | Pass in fixture | Pass in fixture | Three selected bars include the final nanosecond of December 3; a bar before the opening bound and one at next midnight are excluded. This does not replace PR109's application-level acceptance. |
| Two simultaneous strategies / shared cash | Fails intended funding acceptance | Fails intended funding acceptance | Fails intended funding acceptance | The $1,500 account must not fund two $1,000 entries. A process returning successfully is insufficient evidence of a financially valid backtest. See controls below. |
| Staggered entries / shared cash | Fails intended funding acceptance | Fails intended funding acceptance | Pass | RC6 denies the second entry after the first has filled; ending balance is $1,528. This does not resolve simultaneous submissions. |
| Explicit $500 native per-order cap, $1,000 margin order | Does not enforce the cap in this fixture | Does not enforce the cap in this fixture | Pass | RC6 denies the order, produces no fills and preserves $10,000. This is a per-order control, not an aggregate exposure limit. |
| Shared margin with leverage one and 100% modeled margins | Both entries admitted | Both entries admitted | Both entries admitted | All three fail the intended aggregate affordability requirement. |

The simultaneous cash test exposes an important distinction. V1 returns a partial result with two buy fills, zero recorded iterations and a $499 balance. RC6 raises a negative-cash runtime error. Neither satisfies the portfolio acceptance case. This is not evidence that all cash backtests fail or that RC6 is less safe than V1. It is a bounded, reproducible same-bar contention case requiring a supported resolution before shared-capital portfolio validation.

For margin accounts, native accounting and native buying-power enforcement must also be distinguished. With leverage explicitly one and modeled margin one, **all three versions, including RC6, admit both opening orders**; merely setting margin does not establish an aggregate allocation limit. Explicit native per-order caps are a separate mechanism and cannot, by themselves, certify aggregate portfolio exposure. Keep the engine reproduction and validate supported native behavior before inventing an MSAI shadow account/commission engine.

The coordinator independently reran RC6 equity, simultaneous cash and staggered cash: the positive results and the negative-cash exception reproduced. The research matrix contains **21 scenario/version executions**, including expected failures; it must not be described as 21 passing tests.

Two other semantic boundaries remain. In the two-strategy margin case, V1 reports $56 net, while RC6 reports $55.90 because the second sell fills one tick lower. The difference persists with `prob_slippage=0` and explicit `liquidity_consumption=False`. RC6 source contains a one-tick L1 remainder fallback, but the exact reason for a remainder in this abundant-volume fixture is not established. Do not relabel that difference as a validated cost model or a performance improvement.

V1's native return series reports the equity position's 3% price return; RC6 returns 0.28%, matching the account result. RC6 recorded exactly two snapshots, both on December 3, at opening and the last included nanosecond. This constrains the upstream documentation's wording about requiring snapshots across two UTC dates: it is not a universal runtime rule for this exact wheel. Inspect the selected basis and reconcile native equity/returns for every accepted reporting path; neither a report's existence nor its name establishes financial meaning.

## Live lifecycle, persistence and projections

RC6's documented cache-backed `run_async()` restriction was reproduced from the wheel. A native-only blocking `run()` design and a Python data-client hosted design are different contracts; rejection of the latter does not establish that MSAI's native IB/Databento clients lack Redis persistence.

The native-only control ran Python strategies with two native sandbox execution clients and Redis cache, stopped/disposed, recreated the node and loaded **three strategy states and custom cache markers**. A different trader identity did not load them. Account cache keys were persisted, but sandbox reconnect initializes accounts again, so independent account-state restoration was **not proved**. This is a credible native persistence path; open-order/position recovery, crash recovery and real IB reconciliation remain unverified.

A separate hosted node without cache backing ran three strategies over two simulated accounts, emitted three fills/positions and denied an oversized order using a native notional cap. The captured stop handle works during the asynchronous run; calling `node.stop()` directly is incompatible with that borrowed-node lifecycle. V2 has no `node.kernel`, so current readiness checks require supported lifecycle state instead.

Actual Redis records expose an application migration requirement: typed order/position payloads use wrapped maps and singular `events.order.<strategy>` / `events.position.<strategy>` topics. Account balances include currency in their values. Existing MSAI projection logic drops fills/account events or produces empty/zero position fields; old direct cache-reader imports are absent. A small fixture-only conversion recovers three fills, three account updates and three positions, but the existing state index collapses three member positions to two because it omits member identity. Preserve native account/strategy/position and event/trade identities through decoding, persistence and API views; topic changes alone are insufficient.

The unchanged MSAI halt wrapper blocks an opening and permits a real simulated reduce-only exit while preserving sibling positions. Its old USD import, however, returns `None` on RC6, leaving quantitative loss/exposure helpers permissive. The failure-isolation wrapper covers `on_quote_tick`, not V2's `on_quote`: an intentional exception was logged by the native node, which continued delivering quotes, while MSAI never marked that member degraded. Native exception handling and the product's member-failure policy are separate contracts. These wrappers need focused migration, not deletion or assumed compatibility.

The two-account fixture used **different synthetic venues**. It does not certify same-venue multi-account routing, complete account/fleet halt behavior, replay/reconnect deduplication or Databento-to-IB identity.

## Full dependency and Linux packaging boundary

The full backend resolves for both candidates: **137 packages for 1.231**, **132 for RC6**. Resolution preserves existing versions where allowed; it is not a wholesale application dependency upgrade.

The V1 candidate updates PyArrow 23→25, Msgspec 0.20→0.22, IBAPI 10.43.2→10.45.1 and timezone dependencies, while **downgrading IB Async 2.1.0→2.0.1**. The reason is a real dependency intersection: IB Async 2.1.0 requires `tzdata<2026`, while Nautilus 1.231 requires `tzdata>=2026.3`. MSAI's account-summary/portfolio path uses IB Async, so even the smaller bridge needs focused acceptance. Pandas remains 2.3.3 in this full resolution.

RC6 removes the old `ib` extra and several transitive dependencies. In particular, **MSAI directly imports Msgspec**, which disappears from the candidate lock. A real migration must explicitly retain required application dependencies or replace their use through supported native APIs. Adding Msgspec alone cannot repair removed V1 module paths. No direct Fsspec use was found in the inspected production code, strategies or tests. [Exact metadata and source references](2026-10-05-nautilus-upstream-trial-addendum.md#follow-up-complete-application-lock-resolution).

**Both complete `linux/amd64` backend images built successfully** using the tracked Dockerfile, its UV 0.6.0 installer, frozen candidate locks and Python 3.12.15 base. This confirms the dependency/image build path, not unchanged application compatibility. The local builder emulates x86_64 on this arm64 Mac; it is not an Azure-host acceptance test. The images were not published or deployed.

Network-disabled checks identified Python 3.12.15, x86_64 and glibc 2.41 in both images. **All five selected unchanged MSAI modules import on 1.231**. On RC6, the engine and runner module import, while catalog, live configuration, cold-position reader and Databento loader imports fail at removed V1 paths or missing Msgspec. The runner's first 45-second import timed out under load; a focused retry after the builds finished exited successfully. Both observations are retained. A module import does not execute its deferred engine calls. This confirms that a successful V2 image build is not a working unchanged MSAI application.

The **native RC6 equity fixture also passed inside the Linux image**, with networking disabled: three bars, two fills, $28 net and $10,028 ending capital. This extends the macOS native result to the target image architecture, while leaving real application startup and Azure acceptance open. Host CPU/memory saturation made image export/loading slow; these timings are not an engine performance benchmark.

## Native ownership and migration boundaries

The user's Copilot comparison was cross-checked against pinned release source and these runtime results. It reinforces the callback/signal-level migration checks, adds cancellation-scope acceptance and points to an unexecuted benchmark. Its blanket custom-adapter limitation is stale, and its lifecycle summary omits the hosted-cache restriction. [Checked additions and qualifications](2026-10-05-nautilus-upstream-trial-addendum.md#user-supplied-copilot-comparison-checked-additions-and-corrections). This reinforces the direct-V2 development recommendation without changing the remaining acceptance gates.

| Decision | Use in MSAI |
| --- | --- |
| **Reuse native instruments, catalog, simulation and costs** | Wire real asset definitions and expose native venue/fee/fill configuration. Avoid test-equity substitution, a parallel simulator, fee engine or interval scanner. Session/content completeness remains application/provider policy. |
| **Evaluate native portfolio equity and reports** | Persist the native outputs and their basis where they meet the product requirement. Reconcile account capital, fees, open-position value and timestamps before replacing current metrics or tearsheets. Position returns cannot silently become account returns. |
| **Use native live lifecycle, cache and risk primitives** | Adapt process control to supported lifecycle handles and backing-store constraints. Use native limits, while separately proving aggregate account policy, operator halt/resume and failure behavior. |
| **Keep MSAI's control platform** | Research selection/holdouts, immutable evidence, portfolio allocation policy, authorized account assignment, jobs, supervision and intuitive API/CLI/UI are product responsibilities. A native capability does not automatically implement those workflows. |
| **Replace adapters only with proven equivalents** | V1 imports/configuration, event callbacks/envelopes, direct cache readers and readiness internals need deliberate migration. Do not maintain two engine frameworks indefinitely or remove a safety workaround on the strength of an import. |

## Remaining acceptance and recovery constraints

No candidate has completed the real MSAI research/API/CLI/browser journey in this trial. No current data catalog or Redis cache has been converted, no IB reconciliation or actual two-account execution has been attempted, and Azure package/runtime identity has not been freshly inspected. These remain distinct from native fixture passes.

Use separate versioned catalog/cache namespaces for a migration candidate. Preserve the V1 runtime, configuration, original catalogs and saved results. Regenerate candidate catalogs from preserved source data when the supported format requires it; do not rewrite the only copy. Preserve engine version and economic assumptions with historical experiments. Rollback must restore compatible code and state together, not only an image. Before real broker testing, resolve open orders/positions under an explicit controlled handoff; never run competing engine versions against the same account.

The simultaneous-funding failure, event/projection semantics, real strategy behavior, provider-to-broker instrument identity, cold reads, crash/state-loss recovery and real browser acceptance remain gates. No Master Map finding is closed by the existence of a newer engine or by the offline fixtures alone. No speedup or migration-duration promise is supported by this trial.
