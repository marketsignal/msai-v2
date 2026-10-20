# Docker image retention — operator CLI journey

## Surface coverage decision

CLI: required; the retention and installer commands are the operator surface.
API: N/A — this host-maintenance feature has no application API, database or trading behavior.
UI: N/A — this host-maintenance feature has no application UI or browser workflow.

## UC-RET-001: Reclaim an obsolete iteration while retaining recovery images

Actor: Operator maintaining a local MSAI Docker engine.

Scenario: Obsolete research images consume disk alongside current, pinned and container-referenced images. The operator needs to remove an eligible image while keeping recovery paths and unrelated workloads intact.

Intent: Reclaim an obsolete image safely and confirm the retained workload remains intact.

Interface: CLI.

Setup: Use an explicitly pinned disposable/local Docker engine. Through the native Docker CLI import five tiny uniquely labelled fixture images in one nonce repository, add an unrelated second tag to one, and create one never-started network-none container referencing another. Configure only that exact fixture repository, zero newest-version reserve/zero-day fixture grace, explicit current and proven-rollback pins and temporary lock/config. The zero-day grace is fixture-only; production retains seven-day creation grace. Do not pull/build application images or start the container.

Steps:
1. Run the retention CLI without apply and inspect its candidate list and preservation reasons.
2. Apply only the displayed eligible full image ID with `--apply --approve-id <full-ID>`.
3. Repeat the invocation and inspect native Docker image/container inventories.
4. Remove only the exact nonce-labelled fixtures and the never-started container, without force/prune.

Verification: CLI stdout shows exactly one eligible candidate, then that exact approved removed ID. Native Docker inventories show the four preservation cases remain: current pin, rollback pin, container reference and multi-tag image. Every preexisting image ID, container identity/image/state/start/finish/restart count and volume name remains unchanged; no application or data action occurs.

Persistence: The next invocation shows no removed IDs and no eligible fixture candidates. After fixture teardown native inventories exactly match the pre-test baseline.

## UC-RET-002: Preview the real local and VM policy before authorization

Actor: Operator preparing MSAI storage maintenance.

Scenario: Local research iterations and VM deployment versions have accumulated. The operator needs an exact candidate/preservation report before authorizing deletion or persistent maintenance.

Intent: Understand what cleanup would remove and whether space is sufficient.

Interface: CLI.

Setup: Read the operations runbook. Preview installer configs for both hosts with zero arbitrary newest-version reserve and no built-in historical VM pins. Verify explicit context/daemon IDs and retain current/one proven rollback through VM current/last-good authorities or explicit local pins, active local V2 qualification/control pins, required infrastructure and every container reference. Current/last-good may coincide after rollback; the old supervisor stays protected by its container until separately authorized recreation, with an explicit pin beforehand if it must remain afterward. VM report uses strict pinned SSH and existing nonsecret image authorities; temporary helper/config staging is removed normally. No installation, registry pull or service action.

Steps:
1. Run the actual retention CLI report-only against the configured local engine.
2. Run the same reviewed helper report-only on the VM using its exact config and authorities.
3. Inspect full candidate IDs/tags, protected reasons and actual filesystem free bytes; retain the report for the approval decision.

Verification: Stdout shows actual candidates and preservation pins, with removed IDs empty. Insufficient space is shown as DISK_PRESSURE/nonzero status rather than successful cleanup. Mac host filesystem bytes are labelled separately from Docker Desktop internal capacity. No maintenance is claimed active from a source edit or report.

Persistence: Re-invoke report-only before any approved cleanup; new inventory/pins must be checked again. Reports alone never authorize apply or installation.
