# Release safety implementation plan

**Goal:** Allow an operator to update a quiet MSAI installation only after the exact application revision passed its required checks, and reliably close only the temporary SSH access that this repository owns.

**Architecture:** Retain GitHub Actions, the Azure VM, Docker Compose and the current broker supervisor. Add small tested release-control scripts and a read-only fleet readiness response; reuse existing lifecycle records and operator stop controls. No new service, migration, frontend, broker abstraction or release framework.

**Base:** `aa6d44b39f2ad233c49e37161b3f5d50a0d0227d`, branch `fix/release-safety`, isolated worktree. PR #102 remains open; the running local research stack continues using its separate checkout.

**Authority and spec:** User approved this bounded next batch after the necessity/overengineering discussion. MASTER_PLAN M10/M20 supply the release/refusal/access-cleanup requirements. This authorizes local implementation and focused testing, plus already-approved bounded ordinary external reviews. Commit/publication, merge, cloud cleanup and deployment remain concrete final authorization boundaries.

## Diagnosis and existing behavior

The exact current deploy `jq` predicate was executed against fixtures: running blocks, but stopping, unknown status, missing status and empty response all return success with an empty active-ID string. Reproduction is saved locally at `.forge/local/evidence/release-safety/reproduce.py`. `live_status` omits stopping and caps its filtered result at 1000; its unfiltered response is capped at 50 with no pagination/completeness contract. Old responses cannot establish complete fleet state.

The deploy workflow depends on image-build completion, not CI/auth checks, and optional smoke preflight mutates Azure before its current state gate. Cleanup copies suppress deletion errors. The reaper treats prefix/age or missing logs as deletion authority; smoke may still be active beyond that age. The recorded priority-200 conflict and disabled reaper are October audit observations, not refreshed cloud facts.

Source also shows recoverable failed processes can automatically restart. A logical deployment marked terminal does not by itself exclude an active process. Readiness must inspect both tables and conservative pending restart state. It remains a point-in-time precondition, not proof of broker-account flatness or a new atomic maintenance interlock.

Official provider contracts and source links: `docs/research/2026-10-03-release-provider-contracts.md`.

## Design decisions and acceptance criteria

### A. Exact application revision before Azure access

Preserve manual seven-hex SHA resolution and automatic `workflow_run.head_sha`. Resolve once to a full SHA and derive the image tag and target checkout from it. Add an initial release-check job before both preflight and deploy. Workflow-run failure still skips deployment.

Control scripts come from a separate checkout at `github.workflow_sha`; rollback payload Compose/config/deploy-on-vm.sh come from the resolved target SHA. A rollback must never silently load old release-gate helpers.

Require successful **main push** runs on the exact full target SHA for `.github/workflows/ci.yml` and `.github/workflows/auth-gate.yml`. Required job display names are `backend`, `frontend`, `image-data-path`, `Gate 1 — MSAL scope lint`, and `Gates 2+3 — JWT contract + identity-contract lint`. Query by workflow file, SHA, event and branch, without success-only filtering. Check repository/workflow/SHA identity, bounded complete pagination, latest run by creation/id, current attempt, completed success and required jobs. Recheck run/attempt before accepting. Missing, failed, cancelled, skipped, malformed, ambiguous or unreadable evidence refuses; pending evidence waits up to 30 minutes with bounded requests and clear progress. Partial reruns missing required job evidence require rerunning all jobs. Historical rollbacks with missing retained evidence refuse, with no bypass.

Grant Actions read permission only where needed. Run the small infra behavioral tests as a step inside existing backend CI; no new mandatory historical job identity.

### B. Read-only complete fleet readiness

Repair existing `GET /api/v1/live/status?active_only=true` to use the existing five-state active constant including stopping. Preserve dashboard response semantics and its existing cap; do not use that list as release proof.

Add authenticated `GET /api/v1/live/release-readiness` with a typed response:

```json
{"contract_version":1,"scope":"fleet","complete":true,"ready":true,
 "blocking_deployments":0,"blocking_processes":0,"restart_blockers":0}
```

Use one SQL statement with scalar count subqueries, so all counts observe one statement snapshot and have no list truncation. Blocking deployments/processes include every status outside the explicit terminal allowlist (`stopped`, `failed`), including unknown/null status. Process counts cover all process rows regardless of parent state. Restart blockers count failed parents with a latest failed process where `stop_requested_at IS NULL` OR (`failure_kind = 'spawn_failed_transient'` AND (`last_started_at IS NULL` OR `process.started_at >= deployment.last_started_at`)). The second clause covers current-attempt START redelivery, which does not use ordinary auto-restart stop suppression. Do not copy the whole restart policy or assume a paused/retry-delayed process can never resume. An operator's existing stop operation establishes durable stop intent for ordinary failed-process recovery; current-attempt transient failures remain blocked and need supervised resolution. A failed deployment with no process has no restart candidate. Define latest by no process with a later `started_at`, conservatively including tied timestamps, and count each failed parent once. Counts are nonnegative integers, complete is true only after a successful DB read, ready iff all counts are zero. DB failure must not emit a ready response. No account filter is accepted as narrowing release scope.

The release helper uses the authenticated HTTPS endpoint, bounded network timeout, and strictly validates version, fleet scope, completeness, booleans and all counts. Empty/malformed/old responses and any blocker refuse. Explicit fresh-target bootstrap keeps only the existing DNS/connect-error exception; it does not waive CI, allow old contracts, ignore HTTP/auth errors or reinterpret timeouts. Never log the API key.

Readiness runs before optional preflight and again immediately before VM execution, so a long preflight cannot reuse its initial snapshot. Operators must keep starts/resumes suspended throughout the maintenance window and let already-in-flight starts settle; this change does not add a distributed release lock or claim to close every concurrent-start race. It also does not certify broker positions.

**First rollout:** an old API returns no valid readiness contract and the new workflow refuses. Document a separately authorized supervised maintenance installation using the existing VM procedure after fresh complete process/deployment evidence, restart suppression and broker reconciliation. Do not add an `ignore-readiness` switch or use bootstrap against an established target. A rollback to an old API likewise makes future automated releases refuse until a supervised upgrade restores the contract.

### C. One small temporary-rule lifecycle helper

Use a shared Python standard-library helper for deploy/preflight/smoke creation, cleanup and reaping. Keep the existing three rule names/priorities (200/201/202). New ownership descriptions compactly bind version, repository, workflow, run, attempt and phase within Azure's 140-character limit.

Recognize only exact anchored names, matching descriptions, expected priority and inbound Allow TCP SSH-only shape, with a valid runner IPv4 /32 and wildcard destination/source-port constraints. Legacy descriptions may be recognized only in their exact shipped forms plus a successful same-repository GitHub run/attempt lookup proving the expected workflow path, allowed event and branch. Prefix, age and missing activity logs never authorize deletion. Unrelated/malformed/unknown owners remain untouched.

Before creating a fixed-priority rule, list conflicts. If the conflicting rule is demonstrably owned and its exact attempt has completed, reread/revalidate and delete it, prove absence via a successful read, then create. Otherwise explain the conflict and refuse. This must work with the scheduled reaper disabled. Different attempts are different owners; an old completed attempt may be cleaned even while a newer attempt is running. Never delete an active owning attempt.

Separate cleanup jobs remain. They may delete their own exact matching run/attempt/phase rule after the producer dependency has finished, despite the cleanup job itself still running. Failed reads/deletes remain failures with useful diagnostics, not success. Already absent is success only after a successful list proves absence. The reaper uses the same ownership/attempt decision; active and unproved owners remain. No blanket deletion or fallback based on age. Do not print tokens or sensitive command output.

## Bounded implementation tasks

### Task 1 — API readiness (producer API owner)

Own `backend/src/msai/api/live.py`, `backend/src/msai/schemas/live.py`, `backend/tests/unit/test_live_api.py` and a new focused `backend/tests/integration/test_release_readiness.py`. A small dedicated API helper module is allowed only if it reduces route complexity; no other runtime area changes.

- [ ] Write/observe RED for persisted stopping behind >50 newer terminal rows, all other active states, unknown deployment/process status, active process beneath a terminal parent, pending restart, ordinary failed row after durable stop, current-attempt transient failure despite stop intent, tied latest rows, historical older failed process, complete empty/terminal fleet and DB failure.
- [ ] Implement the five-state list fix and the typed untruncated readiness counts above.
- [ ] Run focused tests using a disposable Postgres testcontainer; no shared application DB writes. Add HTTP authentication/response checks and real SQL tests, not query-string assertions alone. Run scoped lint/types.
- [ ] Emit task spec/quality receipts using the actual host runtime task ID.

### Task 2 — release controls and temporary rules (producer release owner)

Own `.github/workflows/{deploy,smoke,reap-orphan-nsg-rules,ci}.yml`, new `scripts/release_safety.py`, `scripts/nsg_rule_lifecycle.py`, `tests/infra/test_release_safety.py`, `tests/infra/test_nsg_rule_lifecycle.py` and existing `tests/infra/test_workflow_deploy.sh`. One owner edits all deploy.yml sections. Keep script logic direct and testable; avoid a configurable policy framework.

- [ ] Write/observe RED behavioral tests executing the actual helpers/callers with controlled external command/API responses: exact A vs green B; pending/failed/cancelled/missing checks; required jobs, reruns/paging/errors; automatic/manual target identity and preflight ordering; strict readiness/old API/bootstrap cases.
- [ ] Implement A/B workflow wiring and helper, preserving target payload identity and current VM deploy procedure.
- [ ] Write/observe RED for all three cleanup callers, inactive owned conflict recovery, active >30-minute owner, wrong/missing ownership, Azure/GitHub read failure, delete failure, verified absence and legacy recognition. Assert mutation call order and preserved rule set.
- [ ] Implement C using shared helper and wire behavioral tests into CI. Replace obsolete text assertions where behavior is now owned by tested helpers; preserve unrelated useful checks. Run focused tests and available workflow/script lint.
- [ ] Emit task spec/quality receipts using the actual host runtime task ID.

### Task 3 — operator guidance and exact-candidate verification (coordinator)

Own this plan, `docs/research/2026-10-03-release-provider-contracts.md`, `docs/operations/release-safety.md`, narrow MASTER_MAP/MASTER_PLAN progress notes, `docs/CHANGELOG.md`, context endpoint/deployment notes, and relevant deployment use cases. Do not mark cloud rehearsal or rollout passed on offline tests.

- [ ] Obtain fresh clean plan review before either producer edits production code. Follow one broad/one repair/one closure review budget.
- [ ] Document exact supported commands, refusal messages, CI rerun policy, operator stop/recheck procedure, first-rollout boundary and disposable Azure rehearsal proposal. API first; deployment CLI next; application UI unchanged.
- [ ] Run preliminary verify-e2e with honest surface coverage. Local helper tests are behavioral acceptance, not a completed GitHub/Azure deployment journey. An actual read-only gate invocation against GitHub and local API may establish limited operator diagnostics; no invented live-state setup.
- [ ] Simplify, stage/freeze, obtain independent final spec/quality reviews and focused verify-app/e2e receipts. A required cloud journey without operational authorization remains unverified and blocks deployment readiness, not unrelated local repairs.
- [ ] Present exact reviewed change plus rehearsal/first-rollout actions for remaining approval. Do not publish or mutate Azure under a test label.

## Operator journey drafts

**RS-1 — select a releasable revision.** Actor: release operator updating the research platform. Scenario: images exist but CI for that revision is pending or failed. Interface: CLI/API (release helper/GitHub). Intent: understand whether the requested version can be installed and what is blocking it. Setup: current authenticated read-only GitHub session, a real observed revision, no cloud access. Steps: invoke exact-revision check; inspect workflow/job identity and refusal/progress; inspect those checks through GitHub and repeat after their state changes. Verification: output names the same revision and required evidence; no Azure action runs on refusal. Persistence: subsequent invocation refers to the same target and fresh workflow state. Negative fixtures remain separate tests, not real provider acceptance.

**RS-2 — establish quiet fleet.** Actor: release operator in an authorized maintenance window. Scenario: strategies have been stopped and operator needs to know whether anything is stopping or can restart. Interface: API/CLI. Intent: avoid replacing services while trading work is unresolved. Setup: intended authenticated target, no new starts/resumes; only sanctioned operator stop controls on explicitly authorized accounts. Steps: inspect status; stop authorized deployments if needed; request release readiness; repeat before update. Verification: receives versioned fleet counts and a clear refusal until all blocking state is resolved; current data is not represented as account-flat proof. Persistence: follow-up read confirms state remains quiet. Broker actions and cloud deployment are not authorized by this draft.

**RS-3 — recover abandoned temporary access.** Actor: release operator on a specifically approved disposable rehearsal NSG. Scenario: a completed run left a rule at a required priority while a separate rule belongs to an active attempt. Interface: deployment CLI/GitHub Actions/Azure API. Intent: recover the update without disrupting another job or leaving access open. Setup: separately approved RG/NSG and controlled owned fixtures created through Azure CLI with genuine workflow identities. Steps: run create/recovery; inspect preservation and new rule; run normal cleanup; reread NSG. Verification: only proved completed-owner conflict is replaced; active/unrelated/unknown rules remain; cleanup failure is visible; successful cleanup proves new rule absent. Persistence: a fresh list confirms the preserved set and removal. Until approved and run, record UNVERIFIED; fake Azure executables are regression tests only.

**Surface coverage:** API readiness and deployment operator CLI/workflows are affected. Application UI has no changes and no new control; no browser redesign/journey is implied. Existing research browser remains running. Real GitHub/Azure mutation journeys require a reviewed operational target and separate authorization.
