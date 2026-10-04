# Releasing MSAI safely

This is the release operating contract. The existing VM/Compose deployment procedure remains in [how_to_deploy.md](../how_to_deploy.md). Local tests, actual provider checks and a production rollout are separate evidence gates. Earlier normal deployments at `fa4c8f8` and `f4ede895` succeeded on October 4 UTC. The later `e3ad095` deployment failed during Azure CLI startup; its focused repair, actual cancellation and scheduled recovery still require acceptance. This document does not authorize cloud changes or trading.

## Choose the environment explicitly

MSAI is in **MarketSignal2**, subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d`, tenant `2237d332-fc65-4994-b676-61edad7be319`. Use **pablo@marketsignal.ai**. KSG/Sponsorship is unrelated; it was still the unqualified CLI default on October 3. Do not change another project's default just to run this procedure.

```bash
az account show --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d --query '{subscription:name,tenant:tenantId,account:user.name}' --output json
```

For MSAI production, the recorded resource group is `msaiv2_rg` and NSG is `msai-nsg`. Verify the intended target on each operation. Every network-rule helper invocation supplies `--subscription`, `--resource-group` and `--nsg-name`; a rehearsal must name its own target explicitly. GitHub Actions obtains the corresponding subscription through its configured deployment variables and OIDC identity.

## Select an application revision

The workflow resolves one full commit SHA and uses it for application files, image tag and required checks. Automatic releases select the completed image build's revision; manual `git_sha` retains the seven-character input resolved through GitHub. The release-control helpers are checked out separately at the executing workflow's immutable `github.workflow_sha`, so an application rollback cannot silently replace them with older guards.

Read-only check, with an existing authenticated GitHub session or appropriately scoped `GH_TOKEN`:

```bash
python3 scripts/release_safety.py check --repository marketsignal/msai-v2 --sha <full-40-character-target-sha> --wait-seconds 1800
```

The required workflows are `ci.yml` and `auth-gate.yml`, on the **same SHA**, main branch, push event. Required job names are backend, frontend, image-data-path, Gate 1 — MSAL scope lint, and Gates 2+3 — JWT contract + identity-contract lint. A build alone is insufficient.

- Pending checks can wait within the bounded budget. Missing, unsuccessful, unreadable or incomplete evidence refuses; a different green revision is not a substitute.
- The newest matching run and its current attempt govern the decision. A partial rerun that lacks a required job needs **Re-run all jobs**. Do not select an older success to conceal a current failure.
- Historical rollback targets need retained successful evidence for that exact revision. Missing evidence has no override; choose an evidence-backed target or prepare a separately reviewed recovery operation.
- Both normal deployment and optional pre-deployment smoke follow the initial checks. The smoke preflight still exercises the currently installed application/data environment, not the candidate image.

## Establish a quiet maintenance window

Suspend new starts and resumes and allow in-flight start requests to settle. Inspect the authenticated API/CLI and use existing stop commands only for the explicitly authorized deployments/accounts. Do not run a fleet emergency stop merely to obtain a green release gate.

```bash
python -m msai.cli live status
python -m msai.cli live stop <authorized-deployment-uuid>
```

The status list remains an operator view; its cap and process-local `active_count` are not complete release evidence. The installed release contract exposes authenticated `GET /api/v1/live/release-readiness`:

```json
{"contract_version":1,"scope":"fleet","complete":true,"ready":true,"blocking_deployments":0,"blocking_processes":0,"restart_blockers":0}
```

The three counts come from one complete database statement snapshot. Nonterminal/unknown deployment or process state blocks, including **stopping**. Processes are inspected independently of parent status. Latest failed-process recovery without durable stop intent also blocks. A current-attempt transient spawn failure can remain a restart blocker even after a stop stamp because START redelivery has separate eligibility; investigate it rather than bypassing the guard.

All counts must be zero and the version/scope/completeness/ready fields must agree. Database, authentication, transport, malformed-response and old-contract errors refuse. The workflow checks again immediately before VM execution after any optional preflight.

**Limits:** readiness is persisted lifecycle evidence at that instant. It is not an atomic release lock, permission to resume/start concurrently, confirmation of actual broker account identity, or account-wide position reconciliation. Reconcile broker state independently and preserve unrelated holdings. Existing account-identity and stop/flatness findings remain open.

The explicit `bootstrap` input is reserved for a verified fresh target/DR rebuild and only retains the existing DNS/connect-refused exception. It cannot waive CI, accept an old API response or bypass HTTP/auth/TLS/timeout failures on an established installation.

## Temporary SSH access

Deploy, preflight and smoke retain priorities 200, 201 and 202 and their per-run/attempt names. A shared helper verifies exact name, description, repository/workflow/run/attempt/phase and SSH-only shape. Legacy names/descriptions need the matching real same-repository GitHub attempt evidence. Age or prefix alone never proves ownership.

Before create, an occupied priority is reusable only if its rule is provably owned and its owning attempt is completed. The helper deletes that rule, proves absence through a successful read, then creates the new one. An active, unrelated or unproved owner is preserved and the conflict is reported. This recovery does not depend on the scheduled reaper being enabled.

Separate cleanup jobs use the producing job's recorded rule identity, including when only cleanup is rerun and GitHub advances the caller's attempt number. The original producer attempt must be completed before a later attempt can remove its rule; only the matching current attempt may use the finished-producer exception. A future, mismatched or unproved owner refuses. Failed Azure reads/deletes remain visible failures. A successful read proving the actual producer rule absent is idempotent success. The reaper uses the same ownership proof; it cannot delete a still-running smoke job merely because it is more than 30 minutes old. Removing a rule closes new access; it does not terminate already-established SSH connections.

The helper's `reap --dry-run` is the preview path. Review its target and decisions before an authorized cleanup. There is no blanket delete fallback for missing GitHub/activity evidence.

### Cancel pending deployment work

The deploy producer uses `!cancelled()` while retaining the exact-revision and
optional-preflight gates. Ordinary cancellation should stop that job; the
separate cleanup jobs retain `always()` and the producer's original rule identity.
Deploy and preflight each have a 30-minute job ceiling. Every SSH/SCP connection
uses strict host-key checking, noninteractive authentication, one connection
attempt and a 20-second connection/handshake timeout. Keepalives every 15 seconds
with three unanswered probes detect an unresponsive peer; they do not limit a
healthy remote command's runtime.

Record the run and attempt before requesting cancellation:

```bash
gh run cancel <run-id> --repo marketsignal/msai-v2
gh run view <run-id> --repo marketsignal/msai-v2 --json status,conclusion,jobs
```

Inspect the **producer job** and its staging/execution steps, then the independent
cleanup job and its `NSG_ABSENT` marker. Run-level `cancelled` alone is insufficient:
the baseline drill [37172097520 attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/2)
accepted cancellation around 02:55:16 UTC on October 4, but still started staging
at 02:55:27 and failed on SSH timeout at 02:57:43. Cleanup removed the owned rule
at 02:58:09. Its producer interruption failed acceptance despite successful cleanup.
The repaired condition and transport bounds have offline regression coverage;
repeat the bounded GitHub drill after integration before claiming runtime acceptance.

Cancellation or SSH disconnect is **not rollback** and does not prove an installer,
migration or container command already started on the VM has stopped. Reconcile
the actual VM/process, image, migration and health state before another install or
recovery decision. NSG deletion stops new connections, not established sessions.
A cancellation test stopped before VM execution proves only that narrower boundary.

### Diagnose an Azure refusal

An `NSG_REFUSED` message retains the failed operation and Azure CLI exit status.
It now adds a bounded symbolic Azure code when available, otherwise a fixed CLI
argument, login, subscription, connection/TLS or runtime-error category. Unknown
formats explicitly remain unclassified. Free-form Azure messages, command
arguments and traceback bodies are withheld; do not turn on raw debug logging
or publish credentials to diagnose a failure.

Only a runtime-error refusal performs one private `az --version` capture with a
five-second timeout. It reports `runtime_cli`, `runtime_core` and `runtime_python`
as bounded numeric versions or `unknown`; Python is the CLI's bundled runtime,
not the runner's system Python. Raw version output, which can contain local paths
and extension details, is withheld. Failed, malformed or ambiguous diagnostics
leave the original operation failed and never trigger a mutation retry.

The fixed `signature=REQUESTS_STRUCTURES_IMPORT_DEADLOCK` token requires a traceback
ending in the known `_DeadlockError` module-lock signature for `requests.structures`.
The exact Azure CLI help footer may follow that terminal exception; arbitrary
trailing text or a later exception does not qualify.
Other tracebacks retain the generic category. This is evidence collection for the
next occurrence, not proof that the earlier generic failures had that cause or
that the upstream CLI issue has been repaired. That diagnostic-only change added
no CLI pin or startup workaround; the subsequently identified failure and focused
startup repair are described below.

Use the code/category to choose the next investigation. It is not proof of a
specific permission or connectivity defect, and does not authorize a retry or
IAM expansion. Inspect the exact target and identity before changing anything.
`reap --dry-run` remains the read-only path for checking preservation decisions.

### Azure CLI startup and its proof boundary

Deploy [37182097303](https://github.com/marketsignal/msai-v2/actions/runs/37182097303)
at `e3ad095` failed on October 4 at 06:29:04 UTC with the exact
`REQUESTS_STRUCTURES_IMPORT_DEADLOCK` signature. A separate bounded version
capture reported CLI/core 2.90.0 and bundled Python 3.14.6. Staging and installer
execution were skipped. Cleanup reported the exact owned rule absent at
06:29:24 UTC and completed at 06:29:26 UTC; independent inventories preserved
all 14 policy fields of four unrelated rules. The failed write response alone
does not prove Azure performed no write. Earlier generic failures remain
unattributed. See the [startup assessment](../audits/2026-10-04/azure-cli-startup.md).

The focused repair configures `MSAI_AZURE_CLI_PYTHON` for the NSG helper. It must
name an absolute path to the CLI-owned interpreter. The three NSG workflows
select `/opt/az/bin/python3` with `AZ_INSTALLER=DEB`; the helper runs it in isolated
mode (`-I`) through the sibling `azure_cli_startup.py`. That entry point completes
Requests import in the same process before Azure CLI can launch command/poller
threads, then runs the real `azure.cli` module with the original arguments.
Both the actual operation and its bounded version diagnostic use this prefix.
Configured empty/relative/unusable runtimes fail closed; only an unset variable
uses the existing native `az` launch. There is no automatic mutation retry or
fallback from a failed configured launch to a second network operation.

For read-only local compatibility checks, select the installed CLI's own
interpreter and its installer mode (`HOMEBREW` for this Mac). Preserve existing
authentication and explicitly select the MarketSignal subscription, resource
group and NSG. Do not substitute the runner's system Python: it may lack the
CLI and its dependencies. The official Docker image uses different RPM/system
packaging and does not certify the affected Debian runtime merely by matching
CLI version. Controlled import reproduction, Mac behavior and actual Linux
runner deployment remain distinct evidence gates. Normal deployment must pass
at the integrated repair revision before the separate cancellation/recovery
drill; operational acceptance stays PARTIAL until all real criteria pass.

Two normal runs at application revision `23db3b8` on October 4 failed before VM
staging: [37164968762](https://github.com/marketsignal/msai-v2/actions/runs/37164968762)
on Azure delete, then [37165202036](https://github.com/marketsignal/msai-v2/actions/runs/37165202036)
on Azure create. Operator recovery separately removed the old completed-owner
orphan. The diagnostic repair does not establish those failures' underlying cause.
Subsequently, [normal run 37171071535](https://github.com/marketsignal/msai-v2/actions/runs/37171071535)
at `fa4c8f8` passed its revision gate, installation, public checks and rule cleanup
at 02:40 UTC on October 4. These observations do not establish unattended reliability.

## First upgrade from the old API

This procedure applies to an old installation or rollback image lacking the readiness contract. The `fa4c8f8` installation passed the contract during the normal October 4 deployment. An old/404 response still deliberately refuses; there is no compatibility shortcut and `bootstrap=true` is not a workaround for production.

Prepare a supervised maintenance installation through the existing [first-deploy/rollback runbook](../how_to_deploy.md), with a concrete approved target and revision:

1. Verify MarketSignal identity, exact target revision checks, backup/recovery material and application/supervisor compatibility. Routine application deployment excludes broker-profile services; do not silently upgrade/restart those services.
2. Establish the no-start/no-resume interval. Use authorized lifecycle controls, then independently inspect complete persisted lifecycle/restart state and actual supervised processes. A capped old status response, a dead API or a stopped container alone is insufficient. Resolve existing broker identity/state ambiguity before any broker-bearing operation.
3. Present the exact installation and rollback commands for authorization. Preserve current image/config identity, and account for migrations: reverting images does not undo schema changes. No automatic maintenance bypass is added by this batch.
4. Perform only that authorized installation. Inspect the new authenticated readiness contract and the normal health/research reference workflow. Record revision, observations and any refused state.
5. Exercise the corrected normal release pipeline under a separately approved bounded rehearsal/rollout. Do not close M10/M20 merely because the endpoint now exists.

A rollback to an old application image may remove the readiness endpoint. Subsequent automated releases then refuse and require this supervised restoration path.

## Bounded recovery acceptance

The operator selected the existing `msaiv2_rg/msai-nsg` for explicitly bounded
recovery checks; no new resource group or rehearsal NSG is required. Preserve all
unrelated rules and record exact workflow-control and application revisions,
run/attempt identities, timings and before/after rule inventories.

October 4 checks removed the old completed-owner orphan and proved cleanup-only
rerun and unreachable-target failure cleanup. The cancellation attempt described
above left its producer running until SSH failed, although its cleanup succeeded.
The final inventory preserved the four unrelated rules. No fresh scheduled-reaper
execution was verified; that remains a separate operational acceptance gate.

After reviewed integration, repeat cancellation with the explicitly authorized
unreachable TEST-NET target, while propagation waits after owned-rule creation
and before staging. Require producer conclusion `cancelled`, skipped staging and
execution, successful independent cleanup, repeated absence of the exact owned
rule and unchanged unrelated policies. Setup failure or a missed cancellation
window is not a passing cancellation test. The ownership guard requires a genuine
`main` run; do not weaken it to test a feature branch. Offline contracts and a
read-only helper preview cannot replace this GitHub/Azure acceptance.
