# Releasing MSAI safely

This is the operating contract for the release-safety candidate. The existing VM/Compose deployment procedure remains in [how_to_deploy.md](../how_to_deploy.md). Local tests, actual read-only provider checks, an Azure rehearsal and the production rollout are separate evidence gates. This document does not authorize cloud changes or trading.

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

The status list remains an operator view; its cap and process-local `active_count` are not complete release evidence. The candidate adds authenticated `GET /api/v1/live/release-readiness`:

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

### Diagnose an Azure refusal

An `NSG_REFUSED` message retains the failed operation and Azure CLI exit status.
It now adds a bounded symbolic Azure code when available, otherwise a fixed CLI
argument, login, subscription, connection/TLS or runtime-error category. Unknown
formats explicitly remain unclassified. Free-form Azure messages, command
arguments and traceback bodies are withheld; do not turn on raw debug logging
or publish credentials to diagnose a failure.

Use the code/category to choose the next investigation. It is not proof of a
specific permission or connectivity defect, and does not authorize a retry or
IAM expansion. Inspect the exact target and identity before changing anything.
`reap --dry-run` remains the read-only path for checking preservation decisions.

Two normal runs at application revision `23db3b8` on October 4 failed before VM
staging: [37164968762](https://github.com/marketsignal/msai-v2/actions/runs/37164968762)
on Azure delete, then [37165202036](https://github.com/marketsignal/msai-v2/actions/runs/37165202036)
on Azure create. Operator recovery separately removed the old completed-owner
orphan. The diagnostic repair does not establish the runner's underlying cause
or certify its write permissions, normal installation or cleanup recovery.

## First upgrade from the old API

The current installation does not have the new readiness contract. The corrected workflow deliberately refuses an old/404 response. There is no compatibility shortcut and `bootstrap=true` is not a workaround for production.

Prepare a supervised maintenance installation through the existing [first-deploy/rollback runbook](../how_to_deploy.md), with a concrete approved target and revision:

1. Verify MarketSignal identity, exact target revision checks, backup/recovery material and application/supervisor compatibility. Routine application deployment excludes broker-profile services; do not silently upgrade/restart those services.
2. Establish the no-start/no-resume interval. Use authorized lifecycle controls, then independently inspect complete persisted lifecycle/restart state and actual supervised processes. A capped old status response, a dead API or a stopped container alone is insufficient. Resolve existing broker identity/state ambiguity before any broker-bearing operation.
3. Present the exact installation and rollback commands for authorization. Preserve current image/config identity, and account for migrations: reverting images does not undo schema changes. No automatic maintenance bypass is added by this batch.
4. Perform only that authorized installation. Inspect the new authenticated readiness contract and the normal health/research reference workflow. Record revision, observations and any refused state.
5. Exercise the corrected normal release pipeline under a separately approved bounded rehearsal/rollout. Do not close M10/M20 merely because the endpoint now exists.

A rollback to an old application image may remove the readiness endpoint. Subsequent automated releases then refuse and require this supervised restoration path.

## Operational rehearsal proposal

The October 3 read-only inventory found only the production NSG; no disposable rehearsal NSG was present. Proposed isolated target: a new **unattached** NSG `msai-release-safety-rehearsal-20261003` in `msaiv2_rg`, eastus2, under MarketSignal2. Do not attach it to a NIC/subnet or alter `msai-nsg` as part of the rehearsal.

After the exact candidate and commands are reviewed, request authorization to create this one NSG, add only the bounded test rules, exercise recovery/cleanup, inspect persistence and remove that same disposable resource. Use genuine recorded workflow-attempt identities for ownership fixtures. Keep active-owner preservation and unknown-owner cases distinct; offline command fixtures alone do not certify the Azure behavior. The full GitHub deployment journey remains a further gate because it also affects an application VM/data environment.

Production orphan `gha-transient-37092738955-1` was still present at priority 200 at 21:37 UTC on October 3; its recorded GitHub attempt was completed/success. That refresh did not inspect every ownership-shape field or delete the rule. Production cleanup and enabling the scheduled reaper are separate named changes with a fresh preview, not side effects of this rehearsal.
