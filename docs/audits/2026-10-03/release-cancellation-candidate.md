# Release cancellation repair — candidate evidence

Recorded October 4 UTC; publication verified at 04:10 UTC, integration at 05:22 UTC and automatic deployment completion at 05:42 UTC. The publication history below is preserved. PR105 is merged and its normal automatic deployment passed; real repaired-runner cancellation and fresh scheduled reaping remain unverified.

## Exact scope

The operator approved fixing the cancellation defect observed in run37172097520,
bounding connection waits and collecting safe diagnostics for the separate
intermittent Azure CLI crash. Implementation is isolated on
`fix/release-cancellation`, based on `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`.
The primary checkout and existing local runtime were preserved.

Frozen repair candidate: `fc06f21a24d85be2979d73907e85eaa798a720216825a93980a77d80c1423f36`.
Git tree: `b687f3642d4660affdf75b99fef2f93c4ae357ec`.
Nine files promoted without source mutation to commit `d5e51977ad86bf6b735684ff03fb8b59fbbf42a6`, with the exact tree above. Published as [draft PR105](https://github.com/marketsignal/msai-v2/pull/105), targeting main. GitHub confirmed OPEN, draft, and the exact head; CI/auth checks were queued or running at 04:10:53 UTC. No merge, deployment, workflow dispatch or cloud mutation occurred in this publication step.

The change replaces the producer's `always()` with `!cancelled()` while retaining
independent cleanup and all revision/fleet gates. It sets a 30-minute deploy job
limit and bounds all eight SSH/SCP invocations. Runtime-error refusals privately
query CLI/core/bundled-Python versions once with a five-second timeout; only
numeric versions and one fixed, strictly recognized import-lock signature can
be emitted. The original operation stays failed and is never retried. This does
not identify or repair the earlier generic CLI crash.

## Verification

- The fresh plan review was CLEAN, using Codex fallback after Claude timed out.
- Producer tests reproduced the missing cancellation, timeout and diagnostic
  behavior before implementation. Independent final verify-app then passed the
  31 NSG and 21 release tests, deployment shell contract, actionlint, shellcheck,
  Python lint/scoped types, strict checks of the two typed parser functions and
  whitespace checks. Candidate identity matched before and after verification.
- Independent final E2E ran the actual documented helper against Azure at
  03:48:02–03:48:09 UTC. The missing-target refusal returned only the symbolic
  ResourceNotFound code; two correct-target dry runs preserved the same four
  rules. The exact final candidate remained unchanged. No rule mutation occurred.
- First final code-spec review was CLEAN. Code-quality review found a concrete
  P2: the fixed Azure CLI help footer follows the traceback, but the parser
  required the known exception at the end of all stderr. Official provider source
  confirmed the format. A complete-stderr regression reproduced this before the
  narrow fix. The repaired candidate now passes 52 tests and fresh real read-only
  E2E; distinct specification and quality closure reviews both returned CLEAN
  at 03:57 UTC, with no remaining findings. The first candidate `1b85a526...`
  and all previous verification remain preserved as historical evidence.

The operator approved exact-candidate draft publication and a separate pre-publication assessment. The independent verifier returned PASS for that scope at 04:07:22 UTC: unchanged reviewed candidate, valid final local evidence and its previously executed real read-only Azure journey. It did not claim a new Azure state observation. Original operational E2E remains PARTIAL in its unchanged report and byte-identical preserved receipt. The canonical checker passed before and after promotion under this approved publication scope; its `SHIP_READY:true` is not full operational release acceptance. All external reviews used fresh Codex fallback after configured Claude attempts timed out.

Actual cancellation remains **PARTIAL**. The real GitHub ownership guard requires
an integrated `main` control revision. The draft RC-1 acceptance requires the
producer itself cancelled before staging, skipped staging/execution, successful
cleanup, repeated owned-rule absence and unchanged unrelated policies. The
run-level cancelled label alone is insufficient. Scheduled reaping remains a
separate unverified operational gate. A fresh metadata check at approximately
03:54 UTC still showed the latest scheduled run as August 10,
[31357080594](https://github.com/marketsignal/msai-v2/actions/runs/31357080594),
on the old `65ae682` revision; it does not certify the current reaper.

## Evidence-recording and publication boundary

Automatic approval review initially rejected required producer self-review receipts as unsupported/fabricated independent review evidence. The Forge role requires task self-reviews, separate from the completed independent final reviews. The operator explicitly approved recording them and publishing this exact candidate. One subsequent write was still rejected for missing genuine evidence. The producer then supplied its concrete criterion-to-test findings and quality self-review; after that evidence was preserved, its canonical native write succeeded and isolated-worktree validation returned exit 0. All denial history remains recorded. No alternative writer, hook change or receipt copy into the primary checkout was used.

The native stop hook had separately looked in the primary checkout although implementation belongs to the isolated worktree. Its routing was not repaired or declared passing; the producer was interrupted after validated artifact evidence was captured to avoid a repeated stop loop.

The fresh authorization was recorded at 04:04:20 UTC and limited to genuine self-review metadata, separate scoped assessment, exact-tree commit/push and a draft PR. Publication succeeded after those checks passed. It does not authorize merge, deployment, new workflow dispatch, network-rule changes or broker activity. The PR was attached to this task. The next release step is CI/review assessment followed by separately authorized integration and the real cancellation/recovery journey; operational PARTIAL has not been relabeled.

At publication, Azure's previously verified installed application baseline remained `fa4c8f8`; no browser/application/trading acceptance was claimed by the infrastructure tests. See the [previous real pipeline evidence](azure-normal-pipeline.md) for the successful installation and failed cancellation baseline.

## Integration and cleanup update, 05:27 UTC

The operator authorized continuation after the concrete merge and automatic-pipeline effect were presented. PR105's CI/auth checks passed. GitHub independently confirmed MERGED at 05:22:03 UTC, exact head `d5e51977ad86bf6b735684ff03fb8b59fbbf42a6`, merge `f4ede89593ff9b8ac68839be0df829bc885cd6cf`. Main image build/auth passed; integrated-main CI and automatic Deploy [37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) were still running at this observation. No new installed baseline or operational cancellation PASS is inferred.

Primary main was fast-forwarded cleanly with every local Forge/context/assessment overlay restored. All four merged worktrees were archived after preserving their evidence. [Consolidation evidence](worktree-consolidation.md) records the new Forge preservation, source-mount handoff, archives and remaining acceptance. Historical candidate receipts remain archival, never active primary gate evidence.

## Normal pipeline completion, 05:42 UTC

[Exact-main CI 37179681442](https://github.com/marketsignal/msai-v2/actions/runs/37179681442), main auth/image build and [automatic Deploy 37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) passed at `f4ede895`. The workflow completed installation, public frontend/TLS probes and ownership-checked transient SSH deletion. This workflow result does not replace a separate direct VM inventory or the actual cancellation/recovery acceptance; operational PARTIAL and M20 remain open. [Consolidation follow-up](worktree-consolidation.md#publication-follow-up-october-4) records the later publication scope and branch cleanup.
