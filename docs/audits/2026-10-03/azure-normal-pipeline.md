# Azure normal deployment acceptance

**Latest assessment: October 4, 2026. Verdict: PARTIAL.** The automatic normal
deployment of `fa4c8f8` passed, and the six Azure application services now use
that revision. Cleanup-only recovery, active-owner preservation and cleanup
after deliberate SSH failure passed. An intermittent Azure CLI runtime failure
and a producer that continues after ordinary cancellation remain open. The
dated sections below preserve earlier failures; the final section records the
latest acceptance evidence. No live-capital readiness is implied.

## Earlier failures — 00:26–00:33 UTC

The two earlier normal-pipeline attempts passed revision and fleet gates but
failed before staging files or executing the VM installer. At that time the
working application remained at `23db3b8`.

| Attempt | Result |
| --- | --- |
| [37164968762](https://github.com/marketsignal/msai-v2/actions/runs/37164968762), attempt 1 | Azure delete exited 1 while recovering an old completed-owner SSH rule. VM staging/install skipped. Cleanup confirmed the new rule absent. |
| Operator recovery | Reviewed ownership-checked helper removed `gha-transient-37092738955-1`. Four unrelated rules were preserved. This used the human operator identity, not the GitHub runner identity. |
| [37165202036](https://github.com/marketsignal/msai-v2/actions/runs/37165202036), attempt 1 | Azure create exited 1 after the orphan was removed. VM staging/install skipped. Cleanup confirmed the intended rule absent; four unrelated rules persisted unchanged. |

Both attempts selected full revision `23db3b838faaed1ab6dddb0d5c0f196f5e0e440d`,
`main`, `workflow_dispatch`, `run_smoke=false`, `bootstrap=false`. Same-revision
CI/auth evidence and authenticated complete quiet-fleet readiness passed.
Absent-rule cleanup does not prove the runner can delete an existing rule.

The OIDC login succeeded with the expected MarketSignal tenant/subscription and
`msai-gh-oidc` identity. Inspection found an NSG-scoped Network Contributor role,
no RG locks and no returned subscription deny assignments. These observations
do not establish successful write authorization. The helper discarded Azure
stderr, so neither run exposes the underlying cause. A bounded diagnostic repair
is being prepared in `fix/release-azure-diagnostics`; no speculative IAM change
or third blind deployment retry was made.

## Preparation and preservation

Before dispatch, the installed Compose, Caddy, renderer and installer matched
reviewed source. A protected in-memory Key Vault render resolved to the same
Compose configuration; registry dependency digests matched installed images.
Schema was already `f6a7b8c9d0e1`; research/ingest queues were empty, jobs terminal,
and the supervisor had no strategy subprocess. The watchdog and backup timer
were active. Root filesystem was 85% used with about 9.3 GiB available.

Fresh protected recovery material remains on the VM under
`/root/msai-release-verification-20261004-23db3b8/` (directory 0700, files 0600):

- `database-before.dump`: 2,898,397 bytes; SHA-256
  `55dba511a58cbc41356dc86f23759660405093ea5e488a7c7c4916c70e113443`.
- `configuration-before.tar.gz`: 42,172 bytes; SHA-256
  `60adcd50473ffb85d36dd8cb050df62e61c3d734ab1cbaece510f4e11d744c58`.

The files were not exported and restoration was not exercised. Earlier image
rollback material remains preserved. The full installer has broader effects
than replacing six app services: environment refresh, dependencies/migration,
vendor smoke, watchdog and backup setup. Both runs failed before any of these.

After the first failed attempt, independent checks found healthy public/API
readiness, authenticated complete fleet readiness with zero blockers, expected
unauthenticated 401, and unchanged research results through API and installed
CLI. The 166-fill reference and legacy null-economics result persisted. No new
broker operation or research submission was part of these checks.

## Acceptance remaining at 00:33 UTC

Normal installation, runner create/delete, active-owner preservation, actual
cleanup-only rerun and cancellation recovery remain unverified. The reaper is
enabled, but no fresh scheduled execution was established. The old orphan is
now resolved by manual recovery; scheduled reaping is a separate open gate.

Next: review and publish the smallest safe diagnostic repair, expose the runner's
actual error category/code, resolve its cause, then complete the planned recovery
matrix. Keep the [first application installation](azure-first-installation.md)
and this failed pipeline evidence separate. No claim of professional-platform
or live-capital readiness follows from either.

## Prepared diagnostic repair — historical 01:18 snapshot

The isolated `fix/release-azure-diagnostics` candidate adds 23 production lines
to retain a bounded Azure code or fixed failure category, without changing
ownership checks, commands or permissions. Candidate
`96a2037e886581dedc1ee202214549774847bdee26fa2014ca0d81d2a300db1e`
passed independent verification of 26 NSG tests, 18 adjacent release-control
tests, lint, typing within the stated legacy-file limits and Python 3.9 grammar.

A real Azure read-only CLI journey failed on the old helper's generic output,
then passed on the frozen repair: a nonexistent NSG reports `ResourceNotFound`,
the correct target reports four preserved rules, and a fresh invocation returns
the same inventory. This demonstrates the diagnostic change under the operator
identity, not successful GitHub runner writes. Both final paired reviews returned
CLEAN/NONE through fresh Codex fallback after Claude timeouts. The complete
candidate-bound evidence set validated, and exact-tree promotion created local
commit `be26c9014086bb79863b662a5539683ee1f20d37`. After explicit user approval,
that exact commit was pushed and [PR104](https://github.com/marketsignal/msai-v2/pull/104)
opened against main. At 01:18:55 UTC its CI/auth checks were running; it is not
merged or installed. Native producer-completion bookkeeping separately
looked in the wrong checkout; its genuine local receipts were preserved, the
looping producer was interrupted, and no hook or receipt was falsified. Final
independent candidate checks passed separately.

At approximately 01:03 UTC, public production health was healthy, remote main
remained `23db3b8`, and no deployment newer than the two failed runs was present.

## Approved integration — historical 01:45 snapshot

Refreshed on October 4 at approximately 01:45 UTC (October 3 local time):

- PR104 remains OPEN at exact head `be26c9014086bb79863b662a5539683ee1f20d37`, base main, merge state CLEAN. PR CI `37167622682`, branch CI `37167602087` and auth gate `37167622752` all completed successfully.
- Remote main remains `23db3b838faaed1ab6dddb0d5c0f196f5e0e440d`. Azure's six application services still use `23db3b8`; the older supervisor and gateway remain in place. Health and ready responses passed, and authenticated fleet readiness reported complete/ready with zero deployment, process and restart blockers.
- The operator said “ok approved, continue.” The exact-head merge attempt was rejected before execution by `.forge/hooks/check-external-mutation-auth.sh`: “external mutation remains human-executed in Forge v1.” A subsequent GitHub read confirmed no merge. No alternate mutation mechanism was attempted.
- Pending action: operator merges PR104 into main using a merge commit, with exact-head guard `be26c9014086bb79863b662a5539683ee1f20d37`. The exact terminal command is supplied in the chat. The authorized-action helper does not provide an allowlisted PR-merge adapter; this is a manual pending-action record, not an authorization receipt or evidence of execution.
- The hook also rejected a documentation patch containing the literal merge command. No files changed on that attempt; this record omits the executable command and preserves the target, strategy and guard.

Integration triggers the existing build/deploy chain. After the operator reports completion, independently verify GitHub's merge SHA and observe that revision's CI/auth/build/deployment. Preserve the earlier failed/PARTIAL reports; normal deployment and runner writes are still unverified. Worktrees, branches and primary local edits remain preserved.

## Operator merge verified — historical 02:28 snapshot

The operator reported completion. Independent GitHub reads confirmed PR104
MERGED at `2026-10-04T02:25:57Z`, merge SHA
`fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`, tree
`637234904cc43c8f85ba3c8ab663e27202da27b5`. The tree equals the frozen,
reviewed diagnostics candidate; parents are the prior main and approved PR head.

At 02:27 UTC, image build `37171036258` and auth `37171036263` passed.
CI `37171036261` and automatic Deploy `37171071535` remain in progress.
These are all bound to the merged SHA. Independent verifier and coordinator
are observing; no duplicate dispatch was issued.

Pre-deployment refresh at 02:28 UTC:

- Explicit Azure lookup confirmed MarketSignal2 and `pablo@marketsignal.ai`.
- The same four unrelated network rules remain; no temporary workflow rule exists yet.
- Authenticated API health, ready and complete fleet readiness passed; all three
  blocker counts zero. System health reports all subsystems healthy and queue depth zero.
  Version/commit fields remain `unknown`, so deployed revision is established from
  actual container images, not those API fields.
- Supervisor process inventory contains only init and its Python parent.
- Protected database/configuration snapshots retain mode 0600, the earlier sizes
  and matching SHA256 hashes. Their restoration remains untested.
- Root disk remains 85% used, with 9.3 GiB available.

## Normal deployment and recovery evidence — from 02:38 UTC

The merged revision is `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7` (reviewed
tree `637234904cc43c8f85ba3c8ab663e27202da27b5`). Main CI
[`37171036261`](https://github.com/marketsignal/msai-v2/actions/runs/37171036261),
auth [`37171036263`](https://github.com/marketsignal/msai-v2/actions/runs/37171036263)
and image build [`37171036258`](https://github.com/marketsignal/msai-v2/actions/runs/37171036258)
all passed before installation.

| Check | Actual result and scope |
| --- | --- |
| Automatic normal deployment, [`37171071535` attempt 1](https://github.com/marketsignal/msai-v2/actions/runs/37171071535/attempts/1) | PASS. Exact-revision gate passed 02:38:13. Runner created its owned rule 02:38:36. Installer completed successfully 02:40:07; public TLS/frontend probes passed. Cleanup removed the rule 02:40:33. Run completed 02:40:38. |
| Real active-owner preservation | PASS. While the normal deployment was active, the reviewed reaper preview and actual invocation both preserved its rule as an active owner, plus all four unrelated rules. This was a supervised operator invocation, not scheduled-run proof. |
| Cleanup-only recovery, [`37171071535` attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37171071535/attempts/2) | PASS. Operator arranged the approved completed-owner fixture `gha-transient-37171071535-1`, TCP 22, priority 200, source 192.0.2.3/32. Rerunning only cleanup job 111345960844 produced cleanup 111346485702, removed the original attempt 1 rule at 02:44:09 and did not create an attempt 2 rule. The installer did not rerun; copied upstream jobs retain their original timestamps. |
| Deliberate SSH staging failure, [`37172008145` attempt 1](https://github.com/marketsignal/msai-v2/actions/runs/37172008145) | Negative recovery check PASS. Target 127.0.0.1 failed host-key verification at 02:46:02; do not describe it as connection-refused. VM installation was skipped. Cleanup 111346847515 removed the real temporary rule at 02:46:26. Overall workflow FAILURE is the expected result of the injected failure. |
| Cancellation setup, [`37172097520` attempt 1](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/1) | FAIL_INFRA. Target 192.0.2.1 was never reached. Rule creation failed at 02:47:30 with fixed diagnostic `category=CLI_RUNTIME_ERROR`; cleanup confirmed the rule absent at 02:47:48. This attempt never exercised cancellation. |
| One supervised repeat, [`37172097520` attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/2) | Cancellation acceptance FAIL_BUG. Rule creation succeeded at 02:55:12 and ordinary cancellation was accepted during propagation. The producer entered SSH staging at 02:55:27, timed out at 02:57:43 and concluded FAILURE at 02:57:48. Cleanup 111348567759 removed the rule at 02:58:09 and passed. The overall run concluded CANCELLED at 02:58:12, which does not prove producer interruption. |

All recovery tests retained the approved revision and existing resource group.
The two failure-test SSH targets cannot reach the production VM. The user's
explicit approval covered the fixture and both failure/cancellation checks;
the repeat preserved the same cancellation inputs. No IAM grant, dependency
change, broker order or new resource group was introduced.

### Installed application and persisted research

Direct VM inspection found all six application services at `fa4c8f8`:

- Backend image ID: `sha256:93954465df102f95347cc51be31e6201f491cac1ccd02edcc548c08024f3983d`.
- Frontend image ID: `sha256:728f0a484634c76dd7789f8afb151bc6ddffff1abcf6619d907aca9e5e87ed60`.
- Migration container exited 0; database head remains `f6a7b8c9d0e1`.
- PostgreSQL, Redis and Caddy retained their existing runtimes. Supervisor
  remains `65ae682`; gateway remains 10.43.1c. Neither broker service was recreated.
- Installer logs show backup timer active, throwaway Key Vault round trip OK,
  backtest smoke PASS, overall smoke PASS and watchdog timer active. Independent
  direct checks also found both timers active. This does not prove restore.

Authenticated health/ready and fleet-readiness checks passed with complete=true,
ready=true and all three blocker counts zero. API version fields still say
`unknown`; actual container identities supply the release evidence.

The existing browser-created reference
[`8ed22dfb-c99b-4ee6-a31d-32eb873d3964`](https://platform.marketsignal.ai/backtests/8ed22dfb-c99b-4ee6-a31d-32eb873d3964)
persisted across deployment. Fresh API and installed CLI processes agree on
166 fills, $1 million opening capital, first-day change −$0.09, closing realized
balance $999,998.79 and total change −$1.21 (−0.000121%). CLI all-trades retrieval
returned 166 rows; report retrieval returned a QuantStats HTML document.

Real-browser computer use reloaded that result through the existing owner
Entra session, observed the native results and opened the rendered full report.
Both display the same economics, realized-balance scope and cost limitation.
These were real API responses; no browser mocks or auth bypass were used.
This is post-deployment persistence/report acceptance, not a newly submitted
research-to-live journey. The earlier real submission is documented in the
first-installation record. Signed report capabilities are not retained here.

### Remaining operational defects

**Intermittent Azure CLI runtime failure:** successful and failed mutation jobs
used runner image 20260927.320.1. Its published inventory lists Azure CLI 2.90.0;
the actual embedded CLI Python runtime was not captured. The fixed category
means stderr contained a traceback, not that its exception type is known.
Azure's activity log, read at 02:55 for 02:45–02:49, showed successful write/delete
events for 37172008145 and no event for 37172097520. Ingestion delay and an absent
event prevent using this to identify the Python exception.

Official [Azure CLI issue 33996](https://github.com/Azure/azure-cli/issues/33996)
reports an intermittent import-lock deadlock, including the NSG create command,
in a different CLI/runtime combination. Proposed [fix 33997](https://github.com/Azure/azure-cli/pull/33997)
was still open/unmerged when checked. This is a credible hypothesis, not a
confirmed MSAI cause or a reason to change versions. The failed runner's raw
stderr was discarded and cannot be reconstructed from retained evidence. Next
diagnostic work should capture actual runtime versions and only a fixed,
allowlisted exception signature in memory; never publish raw tracebacks,
token caches or the whole Azure CLI directory. See the [exact image inventory](https://github.com/actions/runner-images/blob/ubuntu24/20260927.320/images/ubuntu/Ubuntu2404-Readme.md).

**Cancellation does not stop the producer:** the deployed workflow's `deploy`
job uses `always()` with successful-upstream predicates at
`.github/workflows/deploy.yml:348`. Those predicates remain true after an
ordinary cancellation. GitHub's [documented cancellation behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)
retains jobs whose conditions remain true, matching the observed continuation
into SSH staging. The producer also lacks an explicit job/SSH timeout. Keep
cleanup's cancellation-resistant behavior, but make producer cancellation and
connection bounds explicit in a reviewed repair before claiming recovery works.

**Scheduled reaping:** workflow 274196850 is active, but the latest returned
scheduled run remains31357080594 from August 10 on 65ae682. Neither enabling the
workflow nor a manual reaper invocation proves current scheduled operation.
Do not manufacture another orphan simply to obtain scheduler evidence.

At02:50:12, before the cancellation repeat, all 14 recorded fields of the four
unrelated NSG rules matched the baseline (canonical SHA256
`e91b806dd5e741adb2ddcdf7969572d2677aed33fbd3a4f628f0c9ff0cc9ec2d`).
The final post-repeat check at 03:00:26 again matched all 14 fields, the same
four rules and the same canonical hash. All transient test rules are absent.
An independent verifier separately confirmed names/priorities and rule absence
at 02:59:26; its [cancellation report](azure-cancellation-verification.md) retains
FAIL_BUG for interruption and PASS for eventual cleanup. Preserve the unrelated
operator, HTTP, HTTPS and `claude-hvp-session-ssh` policies.
