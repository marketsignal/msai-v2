# Release operational acceptance — October 4, 2026

**Normal integration/rollout: PASS. Actual RC-1: PASS. Genuine scheduled recovery: PASS. Bounded release recovery / M20: PASS at c1dd1c1.**

This is evidence for the bounded startup and release-recovery repair, not professional/live-capital platform or alpha certification. Failed/PARTIAL earlier records remain preserved.

## Exact source and installed runtime

[PR107](https://github.com/marketsignal/msai-v2/pull/107) merged reviewed head `9132c787a9dca715caef9fa3d002eb514c73dad8` at 15:24:53 UTC as `c1dd1c11301b55f6edc20d8751760cc5aa386fe5`. Merged tree `9407558e52d2787f5365b8eaf110d80036f215c2` matches the certified candidate `ab49b8e0e7a4a7b1edb2701acdfb359439f33d76e67fc2319e9acbe2ae535916`.

The exact candidate had 61 focused owning tests, independent spec/quality reviews, verify-app and an actual read-only Mac CLI journey before publication. That preliminary compatibility evidence is separate from genuine Linux acceptance below.

| Check | Actual evidence |
| --- | --- |
| Main CI | [37212905975](https://github.com/marketsignal/msai-v2/actions/runs/37212905975), SUCCESS; backend 3,748 passed, 11 skipped, 17 expected failures; frontend lint/build and image/data-path checks passed |
| Auth / Build | [37212906037](https://github.com/marketsignal/msai-v2/actions/runs/37212906037) / [37212906030](https://github.com/marketsignal/msai-v2/actions/runs/37212906030), SUCCESS |
| Normal deployment | [37213065932](https://github.com/marketsignal/msai-v2/actions/runs/37213065932), attempt 1, SUCCESS at 15:41:05 UTC; exact revision/fleet checks, real NSG create, installer/migration/data-path smoke/watchdog and public TLS/frontend/certificate probes passed |
| Independent normal cleanup | Job 111470405220, SUCCESS; actual owned-rule absence marker at 15:41:02.953 UTC, corroborated by two Azure reads preserving four unrelated policies across 14 fields |
| Installed identities | Six application services directly verified at c1dd1c1; backend digest `cee6781f0dfeba758e94625a41fac102218d7639c39b5633d89d28e6d860e98a`, frontend digest `d796b66441dbb3700137609f0d1913fe4898ed35345e43cbba7df46ac5d155cd` |
| Broker preservation | Supervisor `65ae682` and gateway `10.43.1c` retain exact container IDs, image identities and start timestamps |

Actual Ubuntu producer and cleanup used `/opt/az/bin/python3` with `AZ_INSTALLER=DEB` and performed real network writes/reads. Fresh exact CLI/Python package versions were not emitted, so the older failing 2.90.0/3.14.6 versions are not current successful-run measurements. No manual retry was used.

## Real reference preservation

Installed CLI and independent authenticated GET-only API checks reconciled reference `8ed22dfb-c99b-4ee6-a31d-32eb873d3964`: 166 fills, 83 closed one-share FIFO lots, opening capital $1,000,000, realized change −$1.21, first-day −$0.09 included and closing balance $999,998.79. Actual owner Entra browser reload, native metrics, both fill pages and the full QuantStats report passed with real backend responses. Return −0.000121%, drawdown −0.000251%, Sharpe −3.96 and Sortino −4.91 agree.

Per-fill P&L remains unavailable; decimal FIFO verifies the realized aggregate. Recorded commissions are zero; realistic broker fees/slippage and full marked-to-market NAV remain unvalidated. Legacy reference `6d8e6878-55eb-4b42-8444-6d5a6abc7166` retains 166 records with unknown accounting/P&L/commissions. No new financial job or broker action was submitted for this preservation check. Do not retain signed report URLs/tokens.

## Genuine RC-1 ordinary cancellation

The operator separately approved one bounded manual dispatch at the integrated revision against unreachable TEST-NET `192.0.2.1` in existing MarketSignal2 `msaiv2_rg/msai-nsg`, bootstrap/smoke disabled. [Run 37215116906](https://github.com/marketsignal/msai-v2/actions/runs/37215116906), attempt 1, created at 16:00:04 UTC, is a genuine main `workflow_dispatch`. Its overall cancelled conclusion is expected for this passing cancellation test.

| Criterion | Observed result |
| --- | --- |
| Real owned creation | `gha-transient-37215116906-1`, priority 200, TCP 22, runner source `52.240.168.197/32`; actual marker at 16:00:48.809307 UTC and independent Azure observation |
| Intended cancellation window | Propagation observed in progress at 16:00:50.523510 UTC; one ordinary cancellation requested at 16:00:50.524712 and accepted at 16:00:51.597884 |
| Producer interruption | Actual producer job 111473989745 and propagation step concluded cancelled; producer completed at 16:01:06 UTC |
| No staging/install | SSH setup/trust, both staging steps, installer and public probes skipped |
| Independent cleanup | Job 111474147800 SUCCESS at 16:01:35 UTC; actual absence marker at 16:01:33.118299 UTC |
| Persistence/preservation | Two successful post-cleanup Azure reads prove exact owned-rule absence and unchanged four unrelated policies across 14 fields; policies also match during test; all eight app/broker container identities unchanged |

No retry, force cancel, manual network write, cleanup rerun, VM installation or broker action was performed. This proves ordinary cancellation before staging; it does not prove interruption or rollback of an already-running installer.

## Genuine scheduled recovery — criterion passed

At 16:15 UTC the exact-workflow, schedule-only, exact-c1dd1c1 GitHub query returned no run. The workflow is active, exact integrated file exists and main is default. Latest genuine older-revision schedule was successful run 37212662434 at e3ad095, created 15:21 UTC. Neither workflow enablement, source inspection, an older run nor a manual dispatch proves genuine scheduled execution at the repair revision. The cause of the delay is unproven.

The later genuine [schedule run37218539817](https://github.com/marketsignal/msai-v2/actions/runs/37218539817), main c1dd1c11301b55f6edc20d8751760cc5aa386fe5, attempt1, started16:55:12UTC and completed successfully at16:55:34UTC. Job111483963535 checked out that exact source and its actual reaper step succeeded. Bounded actual logs identify the configured Debian runtime and four NSG_PRESERVED markers. Fresh independent Azure inventory matches the prior16:21:51UTC baseline across all four unrelated rules and fourteen fields. No owned orphan was present: this proves genuine scheduled inventory/preservation, not a new orphan-deletion case. The earlier real owned-rule recovery cases remain separately documented. All three agreed current-revision criteria now pass; close M20 only for this bounded scope.

## Evidence retention and broader boundary

Original failures, final candidate certificates, exact main/deployment metadata, bounded markers, container and network inventories, CLI/API outputs and actual browser observations are retained in the active evidence worktree. A separate primary ignored handoff copy contains 155 SHA-256-verified provenance files, including original state/seed/reviews. Archival receipts are not imported as workflow gates. Preserve evidence and continuity before worktree teardown.

Account identity, supervisor compatibility, storage/restore, data/assets, costs, immutable experiments, selection/holdout, account/portfolio risk and full research-to-live journeys remain open. A later documentation commit changes source revision; it must not borrow these exact-revision CI/deployment results.
