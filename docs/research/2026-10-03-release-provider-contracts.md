# Research: release-safety provider contracts

**Date:** 2026-10-03

**Feature:** Exact-commit release checks and owned temporary SSH-rule lifecycle

**Researcher:** research-first agent

**Boundary:** Official documentation and repository-source research. No GitHub/Azure mutations or live deletion rehearsal. Recommendations below are design implications, not runtime certification.

## Libraries touched

| Dependency | Repository version | Current documented version | Relevant difference |
| --- | --- | --- | --- |
| GitHub Actions REST API | Existing `gh api` calls do not explicitly pin an API version | `2026-03-10`; `2022-11-28` remains supported until 2028-03-10 | Explicitly pin the intended supported contract; this repair does not require a version migration. |
| Azure CLI NSG commands | Hosted-runner CLI, no CLI version pinned by these workflows; Azure login action is separately pinned to v3.0.0 | Official CLI release notes list 2.90.0, dated 2026-09-01 | Actual runner CLI version is unverified. No relevant NSG command removal found; do not infer CLI version from the login action. |

## GitHub Actions REST API

The workflow-runs endpoint accepts a workflow filename and filters including `head_sha`, `event` and `branch`; Actions read permission is sufficient. Responses include run identity, attempt, status, conclusion and repository/workflow metadata. Filtered searches have a 1,000-result cap. The inspected documentation does not specify result ordering. [Workflow runs](https://docs.github.com/en/rest/actions/workflow-runs), accessed 2026-10-03.

Attempt-specific jobs are available at `/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs`, with pagination and Actions read permission. The general jobs endpoint defaults to `filter=latest`; `all` includes older executions. Jobs expose their names, attempt and SHA independently of run conclusion. [Workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs), accessed 2026-10-03.

GitHub supports complete and partial reruns. A rerun retains the original SHA/ref; reruns are available for 30 days. Therefore a deployment policy requiring every mandatory job in the selected attempt may deliberately reject a partial rerun and require rerunning all jobs. It must say so rather than calling old successes current-attempt evidence. [Rerunning workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs), accessed 2026-10-03.

**Design implications:** Query each required workflow for the full target SHA, `push`, `main`, without a success-only filter. Verify returned identities and choose the newest matching run explicitly. Read its current attempt, require successful completion and all named mandatory jobs, then recheck that the run/attempt has not changed. Bound waiting for active runs; fail missing, failed, unreadable or incomplete evidence. Place this gate before every Azure job, including optional preflight. Job-level permission overrides must retain `actions: read` where ownership is inspected. These are proposed fail-closed decisions derived from the documented contracts.

Required names in the inspected source are `backend`, `frontend`, `image-data-path` in `ci.yml`, and `Gate 1 — MSAL scope lint`, `Gates 2+3 — JWT contract + identity-contract lint` in `auth-gate.yml`. REST job names are display names; they are not necessarily YAML job keys. The coordinator intends to add infra behavioral checks as a step in the existing backend job, preserving those required identities for historical targets.

`github.workflow_sha` identifies the workflow file's commit, so an explicit checkout at that SHA can provide the executing revision's control helpers. Keep the rollback/application payload in a separate checkout at the approved target SHA. This distinction matters because `workflow_run` sets `github.sha` to the default-branch commit rather than the upstream run's commit. These workflows currently define their jobs directly; reusable-workflow identity has separate context semantics. [Contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts), [workflow-run event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run), accessed 2026-10-03.

**Tests:** newer failed/pending attempt versus older success; wrong SHA/branch/event/repository/workflow; missing/skipped/duplicate required job; incomplete pagination; rerun between reads; pending timeout; rollback target without retained evidence; all-success control. Exact-main-push policy will refuse unreleased branch SHAs and historical commits without valid evidence.

**Version sources:** [Supported API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions), [breaking changes](https://docs.github.com/en/rest/about-the-rest-api/breaking-changes?apiVersion=2026-03-10), [workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions), accessed 2026-10-03. The new API removes deprecated repository fields such as `has_downloads`; nothing in this repair needs that field.

## Azure CLI and NSG rule lifecycle

The CLI supports list/show/create/delete and a deletion `--no-wait` option. Rule descriptions are limited to 140 characters. [NSG rule commands](https://learn.microsoft.com/en-us/cli/azure/network/nsg/rule?view=azure-cli-latest), accessed 2026-10-03. A delete can be accepted asynchronously; the REST contract distinguishes deleted, accepted and already absent, and returns structured errors otherwise. [Delete contract](https://learn.microsoft.com/en-us/rest/api/virtualnetwork/security-rules/delete?view=rest-virtualnetwork-2025-09-01), accessed 2026-10-03.

Rule reads expose ID, description, protocol, direction, access, priority, source/destination prefixes and ports, including singular and plural forms. The list contract can paginate. [List contract](https://learn.microsoft.com/en-us/rest/api/virtualnetwork/security-rules/list?view=rest-virtualnetwork-2025-09-01), accessed 2026-10-03. Priority conflicts are per direction. Deleting an allow rule affects new connections; it does not terminate an established SSH connection. [NSG behavior](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview), accessed 2026-10-03.

**Design implications:** Use a compact versioned ownership description bound to this repository, workflow, run, attempt and phase, matching an exact anchored name and expected SSH-only rule shape. Neither prefix nor age proves ownership or inactivity. For orphan cleanup, verify the named workflow attempt is completed; preserve unknown/unavailable/deleted GitHub run evidence. Current-attempt cleanup can rely on its known caller identity and a completed producer dependency, after verifying the exact rule; the cleanup job itself being active is expected. An old attempt and a rerun are different owners.

Read the conflicting rule before creation. Delete only a proved owned inactive rule, then successfully reread the NSG to prove absence before creating. Preserve active, malformed, unrelated and unknown rules and fail the conflict clearly. Reuse that ownership decision in scheduled reaping; scheduling is supplementary recovery, not the only recovery route. Keep CLI exit failures visible. A successful empty list may prove absence; a failed list/show cannot. Do not use `--no-wait` to claim completed deletion. Existing legacy descriptions do not contain repository binding. The coordinator intends to recognize only exact historical name/description/shape combinations plus a successful same-repository run/attempt lookup proving the expected workflow path, allowed event and main branch; unavailable evidence preserves the rule.

**Tests:** active owner older than 30 minutes preserved; unknown owner and GitHub/Azure read failures preserved; wrong repo/path/attempt/shape preserved; proved inactive conflict deleted and absent before create; delete failure visible; idempotent absence; all three producer cleanup paths; scheduled reaper disabled. New-rule closure proves the inbound exception is gone, not that historical SSH sessions were terminated.

**Version source:** [Azure CLI release notes](https://learn.microsoft.com/en-us/cli/azure/release-notes-azure-cli?view=azure-cli-latest), accessed 2026-10-03. No CLI upgrade is proposed by this brief.

## Open risks and exclusions

- Documentation establishes provider contracts, not current permissions or production behavior. Independent focused reproduction and controlled runtime verification remain necessary.
- Lookup and mutation are separate operations; rechecking identities narrows races but does not create an atomic GitHub/Azure transaction.
- Exact-main evidence, incomplete partial reruns, unavailable historical run evidence and unbound legacy rules need explicit refusal messages/runbook handling, not silent bypasses.
- No research into Nautilus, data vendors, UI libraries or broker execution was needed for this bounded provider task.
