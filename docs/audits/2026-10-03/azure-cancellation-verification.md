VERDICT: FAIL

# Independent cancellation verification

Recorded from the read-only `diag_e2e` verifier's final report, October 4, 2026.
The coordinator operated the rerun and cancellation; the verifier independently
read GitHub job evidence and the Azure rule inventory. This is operational
evidence, not a replacement for an immutable code-review receipt.

**Cleanup passed, but ordinary cancellation did not interrupt the producer.**
The deployment job continued into SSH staging and ended through a connection
timeout. GitHub's final run-level `cancelled` label does not establish successful
interruption.

- Run: [37172097520, attempt 2](https://github.com/marketsignal/msai-v2/actions/runs/37172097520/attempts/2).
- Revision: `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`.
- Attempt interval: 2026-10-04T02:54:47Z–02:58:12Z.
- Final independent observation: 02:59:26Z.
- Test SSH target: 192.0.2.1; production VM was not the connection target.

| Check | Result |
| --- | --- |
| Temporary rule creation | PASS |
| Ordinary cancellation interrupts producer | FAIL_BUG |
| VM installer remains unexecuted | PASS — explicitly skipped |
| Cleanup after producer timeout | PASS |
| Original and retry rule absence | PASS at final observation |
| Attempt 1 CLI runtime failure | Preserved separately as FAIL_INFRA |

The coordinator issued ordinary cancellation around 02:55:16 after observing
successful rule creation and before staging. The verifier did not issue that
command. Independent job evidence:

| Event | UTC |
| --- | --- |
| Deploy 111348129477 starts | 02:54:50 |
| Rule creation succeeds | 02:55:12 |
| SSH staging begins after cancellation request | 02:55:27 |
| Staging ends with connection timeout, exit 255 | 02:57:43 |
| Deploy concludes FAILURE | 02:57:48 |
| Cleanup 111348567759 starts | 02:57:51 |
| Cleanup concludes SUCCESS | 02:58:11 |
| Overall run concludes CANCELLED | 02:58:12 |

Fixed log evidence: `NSG_CREATED: gha-transient-37172097520-2 priority=200`
at 02:55:12.4769235; `NSG_ABSENT: gha-transient-37172097520-2` at 02:58:09.
The staging error at 02:57:43.3994025 contained the expected TEST-NET target and
`Connection timed out`. The stage ran about 136 seconds; Execute deploy was skipped.

At02:59:26, the verifier found only AllowSshFromOperator(priority 100),
AllowHttpInbound(110), AllowHttpsInbound(120), and claude-hvp-session-ssh(250).
Both attempt 1 and attempt 2 rules were absent. This independent observation
compares names/priorities; the coordinator's separate03:00:26 check compared
all 14 baseline fields and is recorded in the [pipeline dossier](azure-normal-pipeline.md).

The verifier did not inspect implementation source or diagnose a root cause.
The coordinator separately traced `deploy`'s `always()` condition against
GitHub's official cancellation contract, as recorded in the dossier. Attempt 1's
generic CLI runtime failure remains an unresolved, separate finding. No further
retry is needed to establish that this acceptance criterion failed.
