# Azure CLI startup failure and bounded repair

Assessment date: October 4, 2026. Source baseline:
`e3ad0951fbeb9c9d3e85228eafc71bc4c8e56909`. Repair integrated through PR107 as `c1dd1c1`; final candidate certification,
normal Linux rollout and actual pre-staging ordinary cancellation/cleanup passed.
Bounded release recovery is now PASS after genuine scheduled run37218539817 at c1dd1c1; see the current acceptance record. Earlier PARTIAL observations below remain historical.
[Current acceptance and limits](release-acceptance.md). Earlier failure and
prepublication observations below retain their original scope.

## Observed failure

[Exact-main CI 37182068721](https://github.com/marketsignal/msai-v2/actions/runs/37182068721)
passed with 3,748 backend tests, 11 skipped and 17 expected failures, backend
lint/strict typing, release controls, frontend lint/build and image-data-path
checks. Auth and image-build workflows also passed at the same revision.

[Automatic Deploy 37182097303](https://github.com/marketsignal/msai-v2/actions/runs/37182097303)
passed release/fleet checks and Azure OIDC login. Its NSG create step failed at
06:29:04 UTC with the fixed, redacted diagnostic:

```text
category=CLI_RUNTIME_ERROR signature=REQUESTS_STRUCTURES_IMPORT_DEADLOCK runtime_cli=2.90.0 runtime_core=2.90.0 runtime_python=3.14.6
```

Both VM staging steps and installer execution were skipped. This identifies the
exception in this attempt; earlier generic runtime failures remain separately
recorded. A failed CLI response does not establish that ARM performed no write.

The separate cleanup job reported
`NSG_ABSENT: gha-transient-37182097303-1` at 06:29:24 UTC and completed successfully
at 06:29:26 UTC. Independent before/after inventories
matched all 14 captured policy fields for the four unrelated rules. Public
production health remained healthy. No fresh direct VM image inventory was
performed, so the last such inventory remains the earlier `fa4c8f8` observation.
Raw read-only assessment records are preserved locally under
`.forge/local/evidence/release-resume-20261004/` in the primary checkout; these
historical records are not final-candidate certification receipts.

## Repair and proof boundary

The [research brief](../../research/2026-10-04-azure-cli-import-deadlock.md) checks
the matching official issue, the still-unmerged proposed upstream fix and actual
CLI packaging. The [bounded plan](../../plans/release-cli-deadlock.md) proposes
finishing Requests import in the CLI's own main process before command/poller
threads can perform their first import. It retains isolated Python mode,
existing authentication, arguments, time limits, redaction and all rule-owner
checks. It adds no mutation retry, dependency downgrade or IAM change.

The implementation adds an eight-line startup entry point and one shared launch
prefix in the existing NSG helper. Deploy, smoke and reaper workflows configure
the Debian CLI-owned interpreter for all seven NSG paths. Only an unset runtime
selector retains native `az`; explicit invalid configuration refuses without a
second network operation. The same prefix handles the original operation and
its private five-second version capture.

Independent native investigation reproduced the package/submodule first-import
cycle using actual installed Requests 2.32.4 on local Python 3.13.12. This was a
controlled loader interleaving, not an uninstrumented production-frequency test.
Three primary deadlocks and three same-process preload controls passed; a
separate independent native replay reported `REPRODUCED`. The investigator also
compared six actual local CLI 2.83.0 launch cases: JSON version, missing-argument
refusal, help, invalid subscription, authenticated scoped NSG list and empty-auth
refusal. Native and proposed entry-point stdout/stderr/exit behavior matched.
Those observations use the investigator's local bootstrap copy; final shipping
candidate certification is separate. An initial replay receipt was invalidated
by an unrelated protected-state update and is retained; the successful repeat
kept protected state unchanged.

Producer verification observed owning regressions fail before production edits,
then passed 39 NSG/startup and 22 release/workflow tests. The real workflow blocks
used disposable installed-package doubles and asserted same-process main-thread
preload, isolation, argument/environment preservation, diagnostic redaction and
unchanged owner/refusal behavior. Actual Actionlint, ShellCheck, shell syntax,
Ruff and whitespace checks passed. Narrow typing passed while excluding missing
external Requests stubs; no dependency was installed. These fixtures ran on
Python 3.12.12 and their printed Linux/version strings are test data. The actual
authenticated candidate CLI journey and final candidate receipts remain separate.

The preliminary feature journey passed at 07:25 UTC using actual authenticated
Homebrew CLI/core 2.83.0 and its Python 3.13.12 interpreter. The configured helper
refused the missing NSG with a bounded `ResourceNotFound` symbol, then showed four
identical preservation lines in two corrected dry-runs. Independently captured
before/after public CLI inventories matched all 14 fields of the four policies.
No policy write, workflow dispatch, broker action or service change occurred.
The initial sandbox health connection refusal resolved through normal network
escalation. Plan/helper/startup hashes matched before and after. The full leading-
header report and inventories are retained worktree-locally under
`.forge/local/evidence/release-cli-deadlock/preliminary-e2e/`; these are preliminary
observations, not whole-candidate final receipts. UC-CLI-STARTUP-001 is graduated
to `tests/e2e/use-cases/operations/azure-cli-startup.md` with its API/UI N/A rationale.

Source consistency, a controlled import probe, local functional compatibility
and actual GitHub runner acceptance are distinct evidence. The official Docker
image uses RPM/system-Python packaging and cannot certify the affected Debian
bundle merely by matching its CLI version. Record each tested runtime and
instrumentation explicitly; do not infer that an intermittent cloud failure is
fixed from a module-presence check or successful Mac command alone.

## Recovery acceptance history and subsequent closure

The early October 4 assessment found only older August 10 schedule evidence.
Later fresh observation found genuine successful October 4 schedules at `e3ad095`,
latest run `37212662434` created at 15:21 UTC. As of 16:15 UTC, an exact-workflow,
schedule-only query at integrated `c1dd1c1` returns no run. The workflow is active,
its integrated file exists and main is default, but these facts are not runtime
proof. Cause of the delay is unproven. A manual dispatch or older revision does
not prove scheduled execution at the repair revision.

At that pre-scheduled observation, M20 and operational release acceptance remained **PARTIAL** until the repaired
revision passes normal deployment, actual producer interruption with cleanup,
repeated owned-rule absence/unrelated-policy preservation, and fresh genuine
scheduled recovery. These cloud checks require their concrete authorized scope
after the repair is reviewable. No broker action is part of this repair.

Normal Deploy `37213065932` and actual RC-1 `37215116906` at `c1dd1c1` now satisfy the normal Linux and pre-staging cancellation/cleanup criteria, with repeated rule absence and protected-policy preservation. Genuine scheduled run37218539817 subsequently satisfied scheduled execution/preservation at this integrated revision: exact checkout, successful actual step, four preservation markers and independently unchanged fourteen-field policy inventory. Bounded release recovery/M20 is PASS; no new orphan-deletion fixture was present. Current successful logs prove the configured Debian runtime performed actual network operations; they do not freshly emit exact CLI/Python versions. The older failing runtime versions must not be relabeled as current measurements.
