# Azure first installation and research acceptance

**Evidence cutoff: October 3, 2026, 23:58 UTC. Verdict: PASS for the bounded application installation and reference API → CLI → real-browser research journey. Broader release and professional-platform acceptance remain PARTIAL.**

This supersedes the initial October 3 runtime snapshot only for the changes and checks below. It does not erase earlier failures or close unrelated findings.

## Installed revision and integration

The operator explicitly approved the prepared update and manually merged [PR102](https://github.com/marketsignal/msai-v2/pull/102) and [PR103](https://github.com/marketsignal/msai-v2/pull/103). Main is `23db3b838faaed1ab6dddb0d5c0f196f5e0e440d`, with tree `fa62a9865d64be8dbff7552ecac3b8ca89e1077c`, exactly matching the reviewed candidate.

Main checks passed before any service replacement:

- [CI 37162088580](https://github.com/marketsignal/msai-v2/actions/runs/37162088580): backend 3,748 passed, 11 skipped, 17 xfailed, 89 warnings; frontend and image-data-path checks passed.
- [Auth 37162088595](https://github.com/marketsignal/msai-v2/actions/runs/37162088595): passed.
- [Build 37162088597](https://github.com/marketsignal/msai-v2/actions/runs/37162088597): passed.

Build logs, explicit-subscription registry inspection and pulled images agreed:

| Application image | Digest |
| --- | --- |
| Backend and research workers | `sha256:a8a1bb339255c00ffec0501b0b6d7c4aa490a5ffbcdce5681319425d7f108bc1` |
| Frontend | `sha256:d0261f6371e85588c7461d680158cce67ae399e7c767415a02b135012b50c23f` |

The target remained the existing MarketSignal2 subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d`, tenant `2237d332-fc65-4994-b676-61edad7be319`, `msaiv2_rg/msai-vm`. Operator identity is **pablo@marketsignal.ai**; KSG is unrelated.

## Operation and recovery material

The supervised first upgrade replaced only backend, backtest-worker, research-worker, portfolio-worker, ingest-worker and frontend, using the existing Compose file and `--no-deps`. Complete lifecycle/restart/job counts, queued jobs, live-command backlog and actual supervised strategy processes were checked clear immediately before and after stopping those six services. Schema `f6a7b8c9d0e1` already matched. No schema migration, stock installer, vendor download or trading command ran.

All six app services now run tag `23db3b8`. PostgreSQL, Redis, Caddy and the live supervisor retained their container identities and start times. The supervisor remains `65ae682`. The gateway retained its identity/image `10.43.1c`, but restarted at 23:45:03 UTC before upgrade preparation; its cause was not established and uninterrupted broker uptime is not claimed.

Protected rollback settings remain under `/opt/msai/release-23db3b8-backup/`, with old `71aa4a9` images preserved. After acceptance, both `/run/msai-images.env` and `/opt/msai/msai-images.env` were persisted atomically per file with mode/ownership preserved. Only the image tag changed; both final files have SHA-256 `97a3f394972b7257ff23848e9724ec87bb2798a1e9bdd6a9098130416b35f54b`. Rollback was prepared but not exercised. Backup service last reported success; restoration remains untested.

Deploy was disabled during the first upgrade, then restored and independently confirmed `active`. Enabling it created no new run. This was a supervised application installation, not a successful execution of the full normal GitHub-to-VM pipeline. Its cleanup/rerun/failure gates remain open. No production NSG rule was deleted by this installation.

## Independent API and CLI acceptance

Verifier `/root/release_e2e` submitted exactly one existing-data AAPL.NASDAQ backtest, `example.ema_cross`, EMA 5/20, one-share size, December 2 through following-midnight December 7, 2024. Run `437e8bfa-6b9a-4207-b98f-1863a560432b` completed in about 2.87 seconds. An initial invalid readiness enum (`stocks`) was corrected to the documented `equity` before submission; no bypass was used.

All 166 fills across six pages were independently reconciled with decimal arithmetic and FIFO lots:

| Quantity | Result |
| --- | ---: |
| Opening realized balance | $1,000,000.00 |
| Closing realized balance | $999,998.79 |
| Net realized change | −$1.21 |
| First-day realized change | −$0.09, included |
| Executions / closed one-share lots | 166 / 83 |
| Recorded commissions / final inventory | $0.00 / 0 shares |
| Total return | −0.000121% |
| Maximum drawdown | −0.000251% |
| Sharpe / Sortino | −3.96 / −4.91 |

Daily realized changes were −$0.09, +$1.39, −$1.27, −$1.20 and −$0.04. One share remained open on two daily boundaries; realized balance was correctly distinguished from cash flow and full marked-to-market NAV. All daily balances/returns, monthly/headline return, drawdown and primary risk metrics reconciled. New per-fill P&L is unavailable rather than fabricated.

Fresh installed CLI show/trades/report processes exited successfully and matched the API, all 166 records and substantive QuantStats HTML. Repeat reads retained identical results. The preserved legacy run `6d8e6878-55eb-4b42-8444-6d5a6abc7166` retained 166 records and null accounting/P&L/fees through both interfaces.

Result SHA-256: `4874b6215682fb91fe7496c9962fdececdd5b81473a7260cf51e2a8457918ded`; fill SHA-256: `136a5eb45157720c728569b7d7e8c1040175ebc858f67b959bf32cc1ce293c03`. Detailed independent report is preserved in the release worktree at `.forge/local/evidence/release-safety/e2e-postinstallation-api-cli-report.md`.

## Real browser acceptance

Using the existing Entra owner session and real Azure backend, the coordinator entered Backtests → Run Backtest, selected the same strategy/data/parameters and submitted once. [Browser-created run 8ed22dfb](https://platform.marketsignal.ai/backtests/8ed22dfb-c99b-4ee6-a31d-32eb873d3964) completed at 23:53:20 UTC. History, native metrics, both fill pages, full report and page reload passed. The API independently returned the same metrics for this run. Opening capital, realized-balance scope, unknown per-fill P&L and unvalidated broker costs are visible. Legacy results show their warning and unavailable economics. No mocked API routes or auth bypass were used.

The result persisted after reload. Loading states resolved; no duplicate submission was needed. The normal narrow in-app viewport requires scrolling and constrains dense tables/header content; broad responsive/usability acceptance remains separate. Screenshots were inspected through computer use. Signed report URLs and credentials are excluded from this record. This post-installation check did not freshly exercise job failure, cancellation or retry.

## Current support boundary and next gates

Final public health was healthy/production, `/ready` was ready, and authenticated fleet readiness returned contract 1, fleet scope, complete/ready true, zero deployment/process/restart blockers. Unauthenticated readiness access returned 401. Readiness is a point-in-time lifecycle check, not an atomic maintenance lock or account-wide flatness proof. System-health version fields remain unknown; verified image identities establish the installed revision.

This evidence supports using the **named equity reference workflow** to test strategy ideas and inspect honestly labeled outputs. It does not establish calibrated broker costs, full NAV, inclusive-date correctness, data completeness beyond this case, immutable provenance, unbiased holdout selection, correct futures/options modeling, reliable alpha or live-capital readiness.

Next release work: exercise the normal pipeline, real cleanup-only rerun, active-owner preservation and cancellation/failure paths; reconcile production orphan/reaper status. Next research work: data identity, sessions/date bounds, economic assumptions and immutable experiment/holdout controls. Broker identity, supervisor compatibility, account/portfolio isolation and live performance remain prerequisites for multi-account deployment. Backup restoration and storage placement remain open.

The primary local checkout was deliberately left on its earlier revision with existing edits, and the local research runtime was not replaced. These documentary updates do not imply the primary local source equals Azure. Use an intentional checkout reconciliation before the next code batch; preserve both reviewed worktrees and their evidence.
