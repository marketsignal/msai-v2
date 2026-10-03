# MSAI runtime assessment — local and Azure

Observed October 3, 2026, approximately 17:33–17:55 UTC; application source at `7f9eb2b`. This supplements the earlier source/offline audit and includes an actual Azure browser-submitted backtest and subsequent API reconciliation.

Subsequent documentation revision: `docs/agent-context.md` now labels the older LVP-local/HVP-Azure assignment as historical rather than current. References below to conflicting documentation describe the context at inspection time. Correcting that wording does not resolve the broker identity itself. The later [goal-alignment assessment](goal-alignment.md) adds source findings for the clarified multi-account product; it did not perform additional runtime tests.

**The platform exists and important research workflows run. It is not yet a dependable, hands-off research-to-trading service.** Real execution confirmed useful functionality and exposed operational faults that source review alone did not establish. A successful job also returned inconsistent financial measures: execution success and result correctness must be assessed separately.

## What was actually exercised

The coordinator started the existing local PostgreSQL, Redis, API and dashboard, then the backtest, research and portfolio workers. Source mounts point to the current root checkout, replacing the old frontend's worktree mounts. Existing database and market-data files were preserved. Existing dependency images were reused; installed versions match the audited locks (Nautilus 1.223.0, FastAPI 0.133.1, DuckDB 1.4.4, Databento 0.71.0, arq 0.27.0, Next.js 15.5.12, React 19.1.0).

The [local runtime override](local-runtime.override.yml) disables scheduled/vendor ingestion, blocks automatic missing-data ingestion, clears gateway routing, and targets a closed container-loopback broker port. Local broker and live-supervisor containers remain stopped. The API therefore shows unavailable broker account data, as expected. No broker order, Azure deployment, Azure configuration change, database migration, dependency upgrade, commit or push was performed. Normal application reads may synchronize the strategy registry or bootstrap its API user; this was an operational inspection, not an immutable forensic snapshot.

Azure was inspected through existing CLI/GitHub authentication, public endpoints, strictly verified SSH, container metadata, and authenticated public API routes called inside the existing backend. Configured API authentication was consumed in-process without printing its value. Database contents were assessed through application APIs, not manual SQL or Redis reads. A strategy-path resolver was invoked in the existing supervisor without constructing a trading node or connecting a new broker session.

| Real local check | Observed result | What it establishes |
| --- | --- | --- |
| API `/health`, `/ready` | 200 | API liveness and database readiness. Redis/container health and successful queue execution were checked separately. |
| Strategy discovery and validation | Five strategies; EMA validation 200 | Existing Python strategy is discoverable and valid. |
| AAPL bars API | 1,728 bars, January 22–24, 2025 | Existing data passes through the real query layer. No vendor download was needed. |
| Single backtest | `7e10e3c7-74ec-4e99-96df-817ecda44653`, completed, 17.48 seconds worker time | Actual queue → worker → Nautilus → persisted results path works for this equity/window. |
| Fills/results/report | 100 fill rows, three daily series rows, full QuantStats HTML (327,766 bytes) | Real simulated order activity and report generation; not broker execution or correct net economics. |
| Research sweep | `4f39f4ff-3c54-44e1-a190-b0123feba503`, two completed trials, 3.57 seconds worker time | Both actual trials persisted results; no final holdout or investment validation was claimed. |
| Candidate → portfolio simulation | Discovery candidate `6ac330e0-6dbe-4127-8840-5cab08793b83`; portfolio `306c1614-c23b-4770-a252-f0b8bec0a424`; run `2fd27304-967e-4a81-aa03-7087f3404847` completed | Real API composition and one-member quick simulation work. Candidate remains discovery; no live promotion. |
| Portfolio report | Full QuantStats HTML, 327,741 bytes | Portfolio reporting executes; shares the first-day accounting defect below. |
| CLI | `python -m msai.cli backtest show <id>` from `backend/` returned actual status/results | CLI → running API works. Host executable shortcut is absent; root-directory invocation fails as described below. |
| Local browser | Dashboard loads five idle strategies and historical alerts; account unavailable is explicit | Real UI calls work for several surfaces. Backtest history and recent trades displayed fetch failures in the in-app browser despite successful direct API requests. Root cause remains unresolved; do not claim a full browser pass. |

Requests and full non-secret results are in [local-runtime-evidence.json](local-runtime-evidence.json). The requested backtest end date was January 25 to include January 24's session, an explicit workaround for the already identified end-date issue. No future data, invented returns, mocked API responses or hand-seeded database records were substituted.

## Actual Azure browser-to-worker journey

After the operator completed Entra sign-in with `pablo@marketsignal.ai`, the coordinator used the real dashboard's **Run Backtest** form. The selected strategy was `example.ema_cross`, instrument `AAPL.NASDAQ`, fast/slow EMA periods 5/20, quantity one, dates December 2–7, 2024. The following-midnight end date includes the December 6 session. A preceding API check returned 2,400 stored bars across December 2–6; the simulation used existing data.

| Check | Observed result |
| --- | --- |
| Browser submission and persisted run | [Backtest `6d8e6878-55eb-4b42-8444-6d5a6abc7166`](https://platform.marketsignal.ai/backtests/6d8e6878-55eb-4b42-8444-6d5a6abc7166), started 17:48:46.616120 UTC, completed 17:48:49.117739 UTC: **2.50 seconds** worker time. |
| Native results view | Rendered actual metrics, equity curve and 166 fill rows. Reloaded history showed the completed run. |
| Full report view | Actual QuantStats tearsheet rendered in the authenticated browser. A separate API read returned HTML 200, 326,660 bytes, with neither empty nor basic-fallback marker. |
| API persistence | Status, results, all 166 fills and report returned 200. Combined history increased from 11 to 12 records; the single-backtest filter now contains eight. |
| Fill reconciliation | 83 one-share round trips: 20 wins, 61 losses, two flat; **gross PnL -$1.21**. The 24.096% win rate agrees. |
| Financial discrepancy | On the engine's $1,000,000 basis, gross return is `-1.21e-6`; headline `total_return` is approximately `-1.21e-4`, again **100× too large for a ratio**. The series begins with zero return despite a realized nine-cent loss on December 2, and ends at about `-1.1200001e-6`, consistent with excluding that first day. Persisted fill PnL and commissions are all zero. |

This verifies **owner sign-in → browser form → API → queue/worker → Nautilus → persisted results → native dashboard → full report** on Azure for this bounded equity case. It does not establish research selection/graduation, shared-account portfolio execution, fresh vendor ingestion, partner permissions or broker trading. The local and Azure windows and resolved instrument IDs differ, so their return values are not a parity comparison.

Evidence: [complete API responses and report fingerprint](azure-backtest-evidence.json), [browser screenshot](azure-backtest.jpg). No password or signed report token is retained in these artifacts.

## Local versus Azure: observed differences

Counts are application-reported and paginated where noted. Baseline counts precede the new local audit records and Azure browser simulation; do not mistake those records for investment strategies.

| Dimension | Local | Azure |
| --- | --- | --- |
| Application code | Current checkout `7f9eb2b`, hot-mounted | Observed API/frontend/research image tags: `71aa4a9`. Source at that revision has identical application directories and dependency locks to HEAD; the later two commits only change Forge tooling. Built/deployed-byte parity was not established. |
| Trading supervisor | Stopped deliberately | Running `65ae682`, about three months old; incompatible with current canonical strategy paths. |
| Broker | Stopped deliberately | Gateway `10.43.1c`, running and health-check passing; four recorded live deployments all stopped. |
| Identity | Documented development API key | MarketSignal.ai LLC Entra tenant; user confirmed `pablo@marketsignal.ai`, separate from KSG. |
| Strategies | Five | Same five names, environment-specific IDs. |
| Historical market data | Eleven equity symbols, 38 files, 3,761,080 bytes at initial check | AAPL/SPY only, 33 files, 6,563,470 bytes. Neither environment holds the originally proposed multi-asset universe. |
| Backtest history | 115 total before audit (initial page: 20 rows) | Eleven completed records before audit; twelve after the new browser simulation. The original eleven were not replayed. |
| Research/portfolios | Five research jobs and 54 portfolios before audit | Zero research jobs and three portfolios. Database state is substantially different. |
| Recorded live state | Ten stopped, one failed; no running deployment | Four stopped; no running deployment. Neither proves fresh broker-wide flatness. |
| Account registry | Historical local test records | One active registry entry labels LVP `U4705114`, login key `lvp`, mode `live`, account class `real`. This conflicts with documentation saying production uses HVP. |
| Broker-reported identity | Unavailable by test design | `/account/health` reports connected but `account_id: null`. `/account/summary` is 200 with identity null; cached portfolio returns zero rows. Current session/account attribution remains unresolved. |
| Deployment health | Local services now running | Two latest deployments failed on a stale SSH-rule conflict. No application code was replaced by those failed attempts. |
| Storage | Host `./data` and preserved local database volume | Application Docker volumes reside under `/var/lib/msai/docker/volumes`, but `/var/lib/msai` is on the root filesystem. Attached 128 GB data disk has no mounted filesystem. |
| Resources | Seven MSAI containers healthy after start | Standard_D4ds_v6, ~16 GB RAM; ~4.35 GB used at idle, low CPU. Root filesystem 83% used, ~11 GB available. Idle measurements are not load/capacity proof. |
| Backup | Not assessed as a local recovery system | Today's Blob backup exists: PostgreSQL gzip 2,751,925 bytes plus 33 Parquet blobs. Timer success and Blob metadata verified; restore not tested. |

## New operational findings and strengthened accounting evidence

### M20 — Release pipeline is currently blocked

Latest deploys for `7f9eb2b` and `c244426` failed before reaching the VM deploy step. Azure returned `SecurityRuleConflict`: an old `gha-transient-37092738955-1` rule occupies the same inbound priority (200). The scheduled orphan-rule reaper is `disabled_inactivity`, last run August 10. Cleanup reported success but its deletion suppresses errors. This is a current operational defect, not an inferred risk. Exact runs and timestamps: [Azure inventory](azure-runtime.md).

Current HEAD's CI, auth and builds succeeded. Full CI finished after deployment had already started; branch protection requires the two auth jobs but not full CI. That is live evidence strengthening M10. It also distinguishes the earlier host typing/setup failure from the successful GitHub environment.

### M21 — Old supervisor cannot load the new stored strategy paths

Azure's current strategy API returns `example/ema_cross.py`. The running `65ae682` supervisor resolves it under `/app`, while the real file is `/app/strategies/example/ema_cross.py`. Calling the actual installed resolver raised `FileNotFoundError: Strategy file not found: /app/example/ema_cross.py`. The older supervisor does not include the canonical resolver added to the API image in `71aa4a9`.

This proves the deployed path-resolution incompatibility. It does not claim a newly submitted live deployment failed: no live start was attempted. The release contract deliberately excludes broker services from routine deployments, so compatible supervisor maintenance must be explicitly sequenced after account/state checks.

### M22 — Application data is on the wrong disk

`findmnt -T /var/lib/msai` resolves to `/dev/nvme0n2p1`, mount `/`. Docker volume sources for PostgreSQL and application data both live beneath `/var/lib/msai/docker/volumes`. The attached 128 GB device has no filesystem/mount in `lsblk`; the root filesystem is already 83% used. The documented Premium SSD application-data layout is not the running layout. No disk was formatted, mounted or moved during this inspection.

### M23 — Broker identity and version visibility need reconciliation

The production registry says LVP, documentation says HVP, and the account API returns identity null. The null is intentional when a gateway exposes zero/multiple managed accounts; therefore the registry is not proof of the currently connected account. Do not open a local LVP session or restart production trading based on the stale documented split. Production system health also returns `version: unknown` and `commit_sha: unknown`; image inspection was needed to establish versions. Healthy TCP/process checks do not establish safe account attribution or current order capability.

### M24 — Host CLI depends on working directory and leaks configuration in errors

Invoking the CLI from the repository root reads the Compose-oriented `.env`, then fails on nine extra settings. Pydantic's validation diagnostics include input values, including sensitive broker fields. Credential values are deliberately excluded from saved evidence. Running the same CLI from `backend/` with the documented API URL/key succeeds. Fix the settings boundary and redact validation diagnostics; do not copy raw startup errors into issues or reports.

### M05 / M19 — Real fills prove inconsistent return units and missing first-day PnL

The actual backtest has 50 buys and 50 sells, forming 50 one-share round trips: 18 winners and 32 losers, so the reported 36% win rate is correct. The gross cash-flow result is **-$0.53**, consisting of **-$0.04, +$0.38 and -$0.87** over the three sessions.

The engine starts with $1,000,000, so the return ratio is `-5.3e-7`. The API's headline `total_return` is `-5.3e-5`, copying a Nautilus percentage into a field consumed as a ratio: a 100× unit mismatch. The chart/portfolio return is approximately `-4.9e-7`, dropping the first session's four-cent loss. The chart is also rebased to $100,000, rather than showing engine account capital. All fill PnL values are zero placeholders and all saved commissions are zero; source confirms commission persistence is hardcoded. No net-of-cost validity was established. [Exact reconciliation and source evidence](runtime-journey.md#observed-local-result-and-accounting-reconciliation).

The subsequent Azure browser run independently exhibits the same unit and first-day discrepancies, as detailed above. These are now observed in both running environments, not merely source-level hypotheses.

### Browser coverage and its limits

The local dashboard's account failure is expected because the broker was deliberately disabled. Its backtest-history/recent-trades fetch failures are separate: direct API requests succeed and browser preflights reach the backend. A second supported browser was unavailable. This remains an unresolved browser/runtime observation, not a proven defect affecting every browser.

Azure's login page correctly redirects to Microsoft. The preexisting KSG account was rejected as outside the MarketSignal tenant; that is expected tenant isolation. The operator completed Microsoft authentication with `pablo@marketsignal.ai`. The authenticated dashboard displays the owner, five idle strategies, account summary, historical alerts and historical fills. The new browser submission and both results views succeeded as recorded above. No token was forged and no password was requested in chat. This establishes actual owner SSO and the bounded browser workflow; partner-role enforcement remains a separate open finding.

## Local environment left available

Seven local research services remain running: PostgreSQL, Redis, API, frontend, backtest worker, research worker and portfolio worker. API: `http://localhost:8800`; dashboard: `http://localhost:3300`. The local browser limitations above still apply. Existing broker/live-supervisor services remain stopped; vendor ingestion is disabled for this assessment.

To reproduce this guarded startup with the existing images, run from the repository root:

```bash
docker compose -f docker-compose.dev.yml \
  -f docs/audits/2026-10-03/local-runtime.override.yml \
  up -d --no-build postgres redis backend frontend backtest-worker research-worker portfolio-worker
```

This is an assessment configuration using existing data, not the final operating setup for ingestion or trading. Keep the override when resuming these local research services; a bare default startup does not preserve these audit guards. The Azure platform remains available at `https://platform.marketsignal.ai` under its existing configuration.

## Immediate next milestone

Stabilize a bounded equity reference workflow before treating daily research as trustworthy. Owner authentication and the Azure browser backtest now work. First restore release automation, reconcile broker identity and supervisor compatibility, restore-test backups and move application data through a backed-up maintenance procedure to its intended disk. In parallel, correct return units/first-day accounting and explicit cost semantics, and resolve the local browser/CLI problems. Then repair selection/holdout and asset/data contracts before broader strategy research; complete the real research-to-trading acceptance journey only after live-risk/accounting repairs, including restart/recovery and fresh account reconciliation.

The [Master Plan](../../../MASTER_PLAN.md) now places this operational baseline ahead of its broader repair work. The source-audit risks remain findings at their original evidence level unless this report explicitly strengthens them. The runtime results neither prove all prior risks occurred in production nor erase deterministic defects demonstrated offline.
