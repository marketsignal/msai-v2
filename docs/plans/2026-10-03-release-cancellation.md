# Repair deployment cancellation and bound failure diagnostics

Date: October 3, 2026 (America/Chicago; recorded runtime events below are October 4 UTC).
Immutable base: `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`.
Worktree/branch: `fix/release-cancellation`.

## Purpose and authority

The operator approved the next bounded repair: make deployment cancellation
effective, bound connection waits, and capture safe identifying evidence for an
intermittent Azure CLI crash. Preserve the working Azure research platform and
independent ownership-checked SSH cleanup. Existing user approval of staged
acceptance permits preparing a reviewed candidate for later GitHub testing;
publication remains a concrete final action, and real workflow acceptance stays
open until that candidate actually runs. This plan does not authorize trading,
IAM changes, version changes or a new resource group.

## Reproduction and root cause

Actual run [37172097520, attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/2)
at the base revision created `gha-transient-37172097520-2` at 02:55:12 UTC.
Ordinary cancellation was accepted around 02:55:16. The producer nevertheless
started SSH staging to the explicitly approved TEST-NET target `192.0.2.1` at
02:55:27, timed out at 02:57:43, and concluded failure. Cleanup removed its rule
at 02:58:09. Overall run status was cancelled, but the producer was not interrupted.
The independent verifier classified interruption FAIL_BUG and cleanup PASS.
The four unrelated policies were preserved, with all 14 recorded fields equal.

The first incorrect decision is the producer job condition in
`.github/workflows/deploy.yml`: `always()` plus successful upstream predicates
remains true after cancellation. GitHub documents that a true job condition
keeps the job running when cancellation is requested. Unlike the producer,
independent cleanup must remain eligible after cancellation. The producer also
has no explicit job timeout and its SSH/SCP commands rely on default connection
waits. A working control is normal run37171071535, which passed its exact-revision
gate, install and cleanup; preserve those paths and optional-preflight behavior.

Attempt 1 of37172097520 separately failed opening the rule with
`category=CLI_RUNTIME_ERROR`. That proves a traceback occurred; it does not
identify the exception. The known Azure import-lock issue remains a hypothesis.
The diagnostic deficiency is actionable: the helper collapses all tracebacks to
one category and captures neither actual CLI/core nor its bundled Python version.
No retry or speculative upstream workaround belongs in this repair.

Sources and runtime boundaries: [research brief](../research/2026-10-03-release-cancellation.md),
[GitHub cancellation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation),
[expression status checks](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions).

## Minimal implementation

1. **Producer cancellation:** replace the deploy job's `always()` with
   `!cancelled()` while retaining every explicit release-check, optional-preflight
   and trigger predicate. Keep cleanup jobs' `always()` eligibility and recorded
   producer rule identity unchanged. Add a 30-minute deploy job timeout; existing
   preflight remains 30 minutes. Do not weaken exact-revision CI/fleet gates.
2. **SSH/SCP bounds:** every connection in deploy/preflight uses BatchMode=yes,
   StrictHostKeyChecking=yes, ConnectTimeout=20, ConnectionAttempts=1,
   ServerAliveInterval=15 and ServerAliveCountMax=3. Prefer explicit options in
   the existing invocations; no new transport wrapper, SSH config state or remote
   supervisor. Preserve payload paths, override targets, heredoc behavior and
   command failure propagation. The job timeout bounds a live but hung command;
   keepalives detect an unresponsive peer, not a healthy long-running process.
3. **Bounded diagnostics:** recognize only the known traceback signature with
   `_DeadlockError` and an import lock on `requests.structures`; emit fixed labels,
   never extracted message text. Retain `category=CLI_RUNTIME_ERROR` for other
   tracebacks. On that category only, make one best-effort `az --version` capture
   with a five-second timeout. Parse anchored numeric CLI/core/Python versions;
   emit unknown for missing, malformed, ambiguous or failed output. Never publish
   raw version output (which includes paths/extensions), raw stderr, environment,
   auth files or tokens. `az --version` can contact the update service; the bound
   prevents a stalled diagnostic from delaying cleanup indefinitely. The original
   mutation stays failed even when version capture succeeds or fails. No retry
   of the Azure mutation, no new permission, no package installation.
4. **Documentation:** explain ordinary cancellation, time bounds and diagnostic
   limits in the existing release runbook; record the actual failed baseline and
   the distinction between runner cancellation and a remote installer already
   started. After interruption of a real install, inspect actual VM state before
   deciding on recovery; cancellation/SSH disconnect is not rollback or proof of
   remote-process termination. Correct only directly stale release facts in the
   runbook, retaining older failures as dated history.

Changed production/test paths: `.github/workflows/deploy.yml`,
`scripts/nsg_rule_lifecycle.py`, `tests/infra/test_release_safety.py`,
`tests/infra/test_nsg_rule_lifecycle.py`. Documentation: this plan, research brief,
`docs/operations/release-safety.md`, `docs/CHANGELOG.md`, and a bounded operational
use-case record if appropriate. No application, database, broker or frontend change.

## TDD and pre-publication checks

Baseline owning checks: 26 NSG tests and 18 release-safety tests passed on base.

- Before production edits, observe a failing cancellation truth-table regression
  using the actual parsed job expression with cancelled=true. Positive controls:
  manual/automatic releases, successful/skipped preflight and release checks;
  negative controls: failed/cancelled upstream, wrong trigger and cancellation.
  Check cleanup remains eligible when its producer is cancelled and a rule exists.
  Reuse the existing bounded expression fixture, not a new expression framework.
- Exercise real YAML SSH/SCP command blocks against boundary executables; assert
  all calls retain target/payload/host-key validation and carry the chosen time
  bounds. Make a connection failure return nonzero before subsequent staging or
  execution. Check the deploy job timeout explicitly. These are offline workflow
  contracts, not a substitute for GitHub's cancellation engine.
- At the real subprocess wrapper, cover known signature, nearby nonmatches,
  unknown traceback, successful/malformed version output, duplicate fields,
  timeout/missing executable/nonzero exit, and secret-bearing output suppression.
  Assert no mutation retry, no runtime query on success/ordinary ARM refusal,
  unchanged original refusal exit and preserved ownership/lifecycle behavior.
- Run the two owning infra suites, the existing deployment shell contract check,
  focused lint/type checks for changed Python, and workflow syntax validation if
  the existing tool is available. Broad backend/frontend regression belongs in CI.
- Independent plan review precedes implementation; distinct final spec/quality
  reviews and verify-app bind to one staged-clean candidate. Keep any operational
  E2E report PARTIAL until real candidate execution; do not relabel old evidence.

## Acceptance sequence and surface decision

The capability is release operations through GitHub/CLI. Product API and product
UI are N/A: neither exposes deployment cancellation or NSG diagnostics. There is
no product UI change requiring a new browser journey. The working research UI
retains its separate real-browser acceptance evidence.

**Pre-publication:** local code/workflow behavior and safe diagnostic handling can
be verified without production mutation. A real read-only helper preview against
the existing NSG can confirm no regression of the supported operator command;
it cannot certify cancellation or reproduce the intermittent runtime exception.
Independent verification must scope this honestly. The actual GitHub drill is a
post-publication release gate, not a reason to claim E2E complete prematurely.

**Post-publication, supervised candidate drill:** retain the already approved
existing MarketSignal resource group/NSG and TEST-NET connection target, select an
application revision with valid same-revision main CI, and separately identify
the new workflow-control revision. Cancel after successful owned-rule creation
while propagation is running, before real VM execution. Require the producer job
itself to conclude cancelled (not failure after timeout), no subsequent staging,
independent cleanup success and re-read rule absence with unrelated policies
preserved. Record actual run/attempt, control/application SHAs and timings. If
setup fails, retain that failure and bounded diagnostics rather than relabeling
it as cancellation evidence. Do not repeatedly retry until green.

### Operational user journey

- **Actor:** Release operator stopping an in-flight maintenance test before it
  starts deployment work.
- **Scenario:** The reviewed workflow opens its temporary access for an approved
  unreachable test target; the operator decides to stop while propagation waits.
- **Interface:** GitHub CLI/API.
- **Intent:** Stop the pending work and confirm its temporary access is removed.
- **Setup:** Existing sanctioned GitHub/Azure login, exact reviewed control
  revision, same-SHA CI-qualified application target and explicit bounded cloud
  authorization. No pre-created result or rule; rule creation belongs to the run.
- **Steps:** Dispatch the bounded test; observe creation success and its exact
  run/attempt; issue ordinary cancellation; inspect producer and cleanup jobs.
- **Verification:** The operator sees producer conclusion cancelled, staging and
  execution skipped, cleanup success and an absence marker for the owned rule.
  Run-level cancelled status alone is insufficient.
- **Persistence:** Re-request the jobs and NSG inventory; the run is terminal,
  the original owned rule remains absent and unrelated rules remain unchanged.

Full acceptance additionally needs fresh scheduled-reaper evidence. This task
does not claim a remedy for the CLI's unknown underlying crash or unattended
release reliability based on a single successful run.
