# Research: release cancellation and bounded Azure diagnostics

**Final-review clarification, October 4 UTC:** Azure CLI 2.90.0's
[`handle_exception`](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli-core/azure/cli/core/util.py#L142-L147)
attaches a fixed GitHub-issues recommendation to unexpected errors, and
[`print_error`](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli-core/azure/cli/core/azclierror.py#L60-L70)
prints it after the traceback. The known terminal exception may therefore precede
that exact optional footer. Tests must cover complete CLI stderr and retain
negative controls for a different later exception or arbitrary trailing text;
anchoring to the end of all stderr without accounting for the footer misses the
real provider format. This source check does not identify MSAI's earlier crash.

**Date:** 2026-10-03, America/Chicago; cited runtime events occurred October 4 UTC.

**Researcher:** research-first agent. **Base:** `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`.
**Scope:** producer cancellation, SSH bounds and safe CLI evidence. No cloud mutations, package changes or speculative CLI workaround were performed for this research. Sources accessed October 3 local / October 4 UTC.

## Evidence and dependencies

The coordinator's retained evidence records cancellation drill [37172097520 attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/2): rule creation at 02:55:12, accepted cancellation during propagation, SSH staging at 02:55:27, SSH timeout at 02:57:43, producer failure at 02:57:48 and successful rule cleanup at 02:58:09 UTC. An overall CANCELLED conclusion did not mean the producer stopped. Attempt 1 failed earlier with only `CLI_RUNTIME_ERROR`, so it did not exercise cancellation.

| Dependency | Observed or inspected version | Upgrade finding |
| --- | --- | --- |
| GitHub Actions | Hosted service; `ubuntu-24.04` workflow; observed image `20260927.320.1` | Current documented conditions suffice; no action or runner upgrade is required by this repair. |
| OpenSSH client | Image inventory: `1:9.6p1-3ubuntu13.19`; not a fresh process-version observation | Existing connection/liveness options suffice; latest release comparison is outside this repair. |
| Azure CLI | Image inventory: `2.90.0`, also latest published stable at lookup | Actual executing CLI/core/Python versions need direct evidence. No upgrade or downgrade established as a fix. |

Inventory source: [exact runner image](https://github.com/actions/runner-images/blob/ubuntu24/20260927.320/images/ubuntu/Ubuntu2404-Readme.md). Release source: [Azure CLI releases](https://github.com/Azure/azure-cli/releases).

## GitHub cancellation and dependency conditions

GitHub re-evaluates each running job's `if` when cancellation is requested. A still-true condition lets the job continue. For jobs being canceled, the runner signals the local step entry process with SIGINT, waits 7.5 seconds, sends SIGTERM, waits 2.5 seconds, then kills the local process tree if necessary. GitHub documents a further five-minute cancellation ceiling for jobs marked for cancellation. These are platform mechanics, not an application rollback guarantee. [Cancellation reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation).

`always()` remains true after cancellation. `!cancelled()` becomes false and is the documented alternative for ordinary work. Including a status function also replaces the implicit `success()` check; simply deleting `always()` risks reinstating that default. [Expression semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions).

At the inspected base, `deploy` needs both `release-check` and optional `preflight`. Preserve explicit release-check success, allowed preflight success/skipped results, and the event-success condition while replacing the producer's `always()` with `!cancelled()`. This follows the documented expression rules; the real skipped-preflight path still needs acceptance. Keep cancellation-resistant conditions on the separate ownership-checked cleanup jobs. Failed/skipped dependencies otherwise propagate a skip; explicit job conditions control exceptions. Add a finite producer `timeout-minutes` appropriate to the existing installer; its default is 360 minutes. [Workflow syntax: needs and timeouts](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

**Local implications:** check the actual workflow expression for release success/failure, preflight success/skipped/failure/cancelled and canceled/noncanceled workflow cases. Assert cleanup remains separately dependent and cancellation-resistant. A local truth table proves intended logic, not GitHub's scheduling behavior.

## SSH and remote execution boundaries

`ConnectTimeout` bounds connection establishment plus initial SSH handshake/key exchange; `ConnectionAttempts` bounds connection tries. `BatchMode=yes` prevents interactive prompts. After connection, `ServerAliveInterval` and `ServerAliveCountMax` detect an unresponsive peer; the documented example of interval 15 and count 3 disconnects after approximately 45 seconds. They do not impose a runtime limit on a healthy SSH server whose remote command is stuck. Preserve strict host-key verification. [OpenSSH configuration](https://man.openbsd.org/ssh_config).

SCP accepts these SSH options through `-o`; cover every SSH/SCP invocation in the affected producer, including environment staging before the installer. The same options work for SCP's default SFTP transport. [SCP manual](https://man.openbsd.org/scp). The proposed `ConnectTimeout=20`, `ConnectionAttempts=1`, alive interval/count `15/3`, batch mode and strict host-key checking use these existing controls. The proposed 30-minute deploy ceiling remains an acceptance choice, not a documented guarantee of installer completion.

**Local implications:** inspect effective options without connecting (`ssh -G`) and assert every affected invocation receives them; retain host-key refusal as a negative control. Verify the chosen job timeout includes normal installer duration. These checks do not prove delivery of cancellation signals by GitHub.

**Operational limit:** canceling the runner-side SSH process or hitting a job timeout does not establish that a remote installer, migration or container operation has stopped or been reversed. Treat that as unknown until remote state is reconciled. NSG deletion is also not process termination: existing connections survive removal of their allow rule. [Azure NSG statefulness](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview#security-rules). A pre-connection test target proves only pre-install cancellation and access cleanup.

## Azure runtime extraction and fixed exception evidence

The image's system Python 3.12.3 must not be reported as Azure CLI's bundled Python. In CLI 2.90.0, `az version` JSON exposes distribution/extension versions but not Python. `az --version` reports CLI/core and its own `sys.version`; it also contains paths, extension details and an update lookup. [Pinned version implementation](https://github.com/Azure/azure-cli/blob/azure-cli-2.90.0/src/azure-cli-core/azure/cli/core/util.py#L393), [version command reference](https://learn.microsoft.com/en-us/cli/azure/reference-index#az-version).

**Smallest evidence pattern:** only on the existing runtime-error refusal path, run the existing `az` executable once with `--version`, capture stdout/stderr privately with the proposed five-second timeout, and emit strictly parsed numeric CLI/core/Python versions under fixed labels. Anchor package lines; allow their documented optional trailing ` *` update marker. Capture only the numeric version field from the anchored `Python (...)` line, excluding its compiler/build suffix. Bound accepted lengths, reject duplicate/ambiguous fields, and report fixed `unknown` values on failure. Do not echo full lines, paths, environment values or diagnostics, or execute a path parsed from output. Leave successful and ARM-code paths unchanged. Lookup failure must not replace the original mutation error or trigger another mutation.

[Issue #33996](https://github.com/Azure/azure-cli/issues/33996) reports an intermittent `requests.structures` import-lock `_DeadlockError` with CLI 2.89.1/bundled Python 3.14.6 on Ubuntu 24.04; a comment reports the same trace for NSG rule creation. Proposed [fix #33997](https://github.com/Azure/azure-cli/pull/33997) was open and unmerged at lookup. This is a hypothesis for MSAI, not its identified exception. No workaround is justified by the retained generic category.

Match only the known exception/signature in captured stderr and emit a fixed token such as `REQUESTS_STRUCTURES_IMPORT_DEADLOCK`. Require traceback, terminal exception marker and known module-lock text together, with line structure checked; never copy arbitrary exception names/messages or the memory address. Preserve existing bounded Azure-code precedence and generic fallback. Tests should include matching traces, unrelated tracebacks, isolated marker fragments, secret-bearing surrounding text, update markers, malformed/oversized version fields, duplicates, version-command timeout and nonzero exit. Assert exact safe output and no write retries.

Activity Log metadata can distinguish a recorded ARM operation from a client-side failure, but cannot reconstruct a discarded Python traceback. Entries normally appear after 3–20 minutes; absence alone is inconclusive. [Activity Log timing](https://learn.microsoft.com/en-us/azure/azure-monitor/fundamentals/activity-log). No historical exception is newly proved by this parser.

## Required later acceptance and exclusions

After review and authorized integration, a real GitHub run must show the optional-preflight skip still permits deploy, then ordinary cancellation after rule creation stops the producer before VM staging/installation and its separate cleanup proves rule absence while preserving unrelated rules. Record exact workflow revision, attempt, cancel request, producer/step conclusions and UTC timings. A pre-create CLI failure is not cancellation acceptance; cancellation after a remote installer starts is a separate unproved recovery boundary.

This research does not certify live trading, remote rollback, scheduled reaping or a repair of the intermittent CLI failure. No IAM changes, dependency pinning, import shim, additional resource group or general diagnostic-log upload is supported by the current evidence.
