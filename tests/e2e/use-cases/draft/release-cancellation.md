# Release cancellation — draft operational acceptance

Status: DRAFT. Cancellation is pending execution against the integrated repair.
Reviewed plan: `docs/plans/2026-10-03-release-cancellation.md`.
Repair base: `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`.

This file defines acceptance; its presence is not execution evidence or authority
to publish, dispatch, cancel, create rules or change Azure resources.

## Surface coverage decision

- CLI: Covered through the existing GitHub CLI, Azure CLI and documented NSG helper.
- API: N/A — the product API does not expose deployment cancellation or Azure NSG
  diagnostics. GitHub provider responses observed through `gh` support the CLI
  journey; they are not product API acceptance.
- UI: N/A — the application browser does not expose this release-administration
  operation. Existing research-browser evidence remains separately scoped.

## Execution set and evidence boundaries

### Read-only regression before publication

Reuse **UC1: Diagnose a failed inspection and inspect the intended target** from
`../operations/azure-failure-diagnostics.md`, without duplicating or weakening it.

Run that existing UC against the settled candidate helper and real Azure using
only `reap --dry-run`, the explicit MarketSignal subscription and existing
sanctioned identity. Preserve its missing-target error, correct-target preview
and reinvocation assertions.

A PASS establishes only the observed read-only operator regression. It does not
prove cancellation, bounded runtime diagnostics on an actual runner crash, or
scheduled recovery. Record the exact candidate identity before and after.

### Operational cancellation after integration

Execute RC-1 below against the integrated workflow-control revision. Record that
control SHA separately from the CI-qualified application SHA.

The overall operational E2E assessment remains PARTIAL while RC-1 is unexecuted.
Do not convert a local workflow-expression test, mocked subprocess test, successful
preview, publication or overall cancelled run label into RC-1 PASS.

## RC-1: Stop pending deployment work and confirm temporary access is removed

- **Actor:** Release operator stopping an in-flight maintenance test before VM
  staging or installation begins.
- **Scenario:** The reviewed workflow has opened temporary SSH access for the
  approved unreachable TEST-NET target. During propagation, the operator decides
  to stop the pending work and needs the producer stopped and access removed.
- **Interface:** CLI, using GitHub and Azure provider responses through `gh` and `az`.
- **Intent:** Cancel pending deployment work and confirm that its temporary access
  has been removed without disturbing unrelated network policies.
- **Setup:**
  - Existing sanctioned GitHub and Azure sessions. Confirm operator
    `pablo@marketsignal.ai`, MarketSignal2 subscription
    `68067b9b-943f-4461-8cb5-2bc97cbc462d`, resource group `msaiv2_rg`,
    NSG `msai-nsg`. Do not change default subscription or authenticate another identity.
  - Resolve the integrated reviewed workflow-control SHA and its dispatch ref.
    Resolve the exact application SHA with the required successful main CI/auth
    evidence. Record both; do not assume they are interchangeable.
  - Confirm the explicit authorization and supervision for this concrete drill,
    the required quiet-fleet maintenance conditions, and target `192.0.2.1`.
    No production VM execution, broker operation, IAM change or new resource
    group belongs to this journey.
  - Read the current NSG policy baseline through Azure CLI before dispatch.
    Retain unrelated rule names and policy values, including priority, direction,
    access, protocol, port/address selectors and description. Do not remove or
    alter existing rules to arrange the test.
  - No rule or completed result is pre-created. The workflow owns rule creation.

- **Steps:**
  1. Dispatch the existing deployment workflow through the documented CLI with
     the resolved control ref and application target:

     ```text
     gh workflow run deploy.yml --repo marketsignal/msai-v2
       --ref <reviewed-integrated-control-ref>
       -f git_sha=<CI-qualified-application-sha>
       -f vm_public_ip=192.0.2.1
       -f run_smoke=false
       -f bootstrap=false
     ```

     Resolve the resulting run from fresh dispatch metadata. Record its full
     workflow-control/application identities, run ID and attempt. Placeholders
     must be resolved before execution.

  2. Observe the same run/attempt through `gh run view` or the attempt-specific
     `gh api` jobs response. Confirm release-check success, optional preflight
     skipped as requested, and successful owned-rule creation. Record the exact
     rule name and creation timestamp. Actively supervise the short propagation
     window; verify VM staging has not started.

  3. While propagation is still running, issue ordinary cancellation for that
     exact run:

     ```text
     gh run cancel <run-id> --repo marketsignal/msai-v2
     ```

     Record the request time and CLI result. A successful request is not yet
     evidence that the producer stopped. Do not use force-cancel to satisfy this
     ordinary-cancellation journey.

  4. Inspect the actual producer and separate cleanup jobs for the same attempt.
     Record producer/step conclusions, start/completion times and bounded lifecycle
     markers from completed logs. Distinguish newly executed jobs from carried
     records by their timestamps if a rerun is involved.

  5. After cleanup completes, list the NSG rules through Azure CLI with the explicit
     subscription/resource group/NSG. Compare the owned-rule absence and unrelated
     policy values with the pre-dispatch baseline.

- **Verification:**
  - `gh run view` stdout or the `gh api` response shows the producer job itself
    concluded `cancelled`. Its VM staging and `Execute deploy` steps were skipped;
    no staging step began after the recorded cancellation request.
  - The producer did not merely finish with failure after a natural connection
    timeout. Overall run conclusion `cancelled` alone is insufficient.
  - The cleanup job concluded success. Its bounded log shows an absence marker
    for the exact owned run/attempt rule that was previously created.
  - Azure CLI stdout lists no matching owned rule. The unrelated policies match
    the pre-dispatch baseline; do not treat names alone as full policy comparison.
  - The new control revision and actual run/attempt are present in the evidence.
    Old baseline results do not certify the repaired revision.

- **Persistence:** Re-request the same attempt's jobs and repeat the explicitly
  scoped Azure CLI inventory. The producer and cleanup remain terminal, the
  owned rule remains absent, and unrelated policy values remain unchanged.
  Record concurrent authorized changes as interference rather than modifying
  their rules or claiming an unchanged baseline.

## Classification and limits

- If ordinary cancellation is accepted in the required window but the producer
  continues into staging or ends through natural timeout, record FAIL_BUG even
  if overall run status is cancelled and cleanup eventually succeeds.
- If NSG creation or another setup dependency fails, retain that failure and its
  bounded diagnostic. Cancellation was not exercised; do not call it PASS.
- If the required cancellation window is missed, record the incomplete timing
  condition and actual outcomes rather than certifying this journey.
- Preserve the prior attempt's CLI_RUNTIME_ERROR as separate historical evidence.
  A later successful creation does not identify or repair its underlying cause.
- Do not repeatedly retry until green, expose raw diagnostics, alter credentials,
  change permissions or inject a runtime crash to obtain desired evidence.
- This pre-installation journey does not certify remote installer termination,
  rollback, backup restoration, live trading or scheduled-reaper execution.
- Fresh scheduled-reaper evidence remains a separate broader operational gate.
