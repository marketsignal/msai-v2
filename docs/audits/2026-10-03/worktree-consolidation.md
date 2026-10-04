# Merged-worktree consolidation

Initial consolidation recorded October 4, 2026, through 05:27 UTC, with the publication follow-up below. Old-worktree cleanup is complete; release recovery acceptance remains PARTIAL.

## Integration and remaining checkout

The operator authorized removal of stale merged worktrees and clarified that the new Forge harness belongs in current working checkouts, without retaining obsolete copies. GitHub and Git independently verified this merge order:

| PR | Change | Merge on main |
| --- | --- | --- |
| 102 | Research accounting and operator workflow | `4b8f0823b559f8485fdd20dcb3226da93215bb87` |
| 103 | Release revision, fleet and cleanup guards | `23db3b838faaed1ab6dddb0d5c0f196f5e0e440d` |
| 104 | Bounded Azure failure diagnostics | `fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7` |
| 105 | Cancellable deployment and bounded diagnostics | `f4ede89593ff9b8ac68839be0df829bc885cd6cf` |

PR105 merged at 05:22:03 UTC with the exact reviewed head `d5e51977ad86bf6b735684ff03fb8b59fbbf42a6`, after its PR checks passed and the operator authorized continuation. Its automatic image build and auth gate passed; integrated-main CI and Deploy [37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) were still running at this observation. No new installed Azure revision is inferred.

All four managed feature worktrees are archived recoverably in Codex. Git now lists only `/Users/pablomarin/Code/msai-v2`, branch `main`, at `f4ede895`. The four local feature branches were deleted with safe deletion after verifying their commits were ancestors of main. Remote branches were retained.

The operator's Forge 6.4.3 installation remains in the primary checkout. Its 35 modified harness/adapter paths matched the final worktree byte-for-byte before archival. These are **preserved local changes**, not a claim that Forge 6.4.3 has been committed or deployed by PR105. Assessment/context edits also remain preserved locally.

## Preservation and continuity

Ignored evidence was exported outside every worktree, under:

`/Users/pablomarin/.codex/worktree-evidence/msai-v2/2026-10-04/`

Each archive has a JSON manifest with original path/HEAD and per-file SHA-256 hashes; archive contents were verified against those hashes. Native worktree archival separately preserves tracked local changes.

| Archive | Files | Archive SHA-256 |
| --- | --- | --- |
| `research-foundation-ignored-evidence.tar.gz` | 60 | `a98aa7c4c0223e0f8c0e2b405aab8ffceaf91c44a4c3043833ad12951fab4ab0` |
| `release-safety-ignored-evidence.tar.gz` | 77 | `c5916b10aa4e18eb8536369765d0b0b696b12baa173b8fd84b1b731a7eae2852` |
| `release-azure-diagnostics-ignored-evidence.tar.gz` | 48 | `68aea6d39992564e3eac9949aa75c436e819c0e5d5aaa26184dfe4938ff7060b` |
| `release-cancellation-ignored-evidence.tar.gz` | 72 | `aeb00cd6b9d46b96800228e4a46c1ccb56e77e4fe9e03db41964b66a81f1ad4c` |

Canonical narrative fold returned `FOLD_OK` for research-foundation and `FOLD_DIVERGED` for the other three. Divergent narratives were not overwritten; original state and seed files are preserved in these archives. Gate receipts were not imported into primary as active evidence.

Primary synchronization used clean stashes and verified overlay backups, with fast-forward only and exact file restoration. Both stashes remain recoverable. Before the first synchronization, all 57 original local files were preserved in `primary-before-sync.tar.gz` (SHA-256 `9e911a0a6fd73f45b1ecd221501e1bf76aa2a701de3855ec2353523cdfec5918`). Before PR105 synchronization, all 44 remaining modified/untracked files were preserved in `primary-before-pr105-sync.tar.gz` (SHA-256 `8a3a2ff1fc2a1661d7e881639fd512ee62ed91b0e53c95f39fef17578bd18ceb`). Original bytes and modes were restored and checked.

## Local runtime handoff

Five running local services depended on research-foundation source mounts. Before removing that checkout, backend, frontend and three research workers were recreated using existing images and the same environments/data, with source mounts redirected to primary through [the stable override](local-runtime-source.override.yml). PostgreSQL and Redis were preserved. The seven intended research services are healthy; broker, supervisor, watchdog and ingestion services remain stopped. No container retains a mount into an archived worktree.

Actual environments retained empty vendor credentials, disabled scheduled ingestion/healing and closed-loopback broker access. No order, vendor download or new research job was submitted during this handoff. Three old May portfolio rows still marked running remain an operational finding; they were not changed for cleanup.

After the handoff, local `/health`, `/ready` and the existing backtest page returned HTTP 200. Computer-use reload of real backtest `29e190ef-8b42-4115-90bc-34c100742597` retained the completed result, 100 fills, $1 million opening capital and −0.0000530% return. Realized-balance scope, fees caveats and unknown legacy fill economics remained visible. This verifies the source/persistence handoff, not broad research, Entra or broker acceptance.

## Next acceptance

The integrated-main CI and automatic deployment at `f4ede895` have passed, as recorded below. Next run the separately scoped real cancellation and scheduled-recovery checks. Preserve original failures and operational E2E PARTIAL until the required real outcomes are proved. M20 remains open; cleanup and merging do not confer trading readiness.

## Publication follow-up, October 4

After the operator explicitly requested GitHub branch removal, each old branch tip was checked as an ancestor of `f4ede895`. The four branches `fix/research-foundation`, `fix/release-safety`, `fix/release-azure-diagnostics` and `fix/release-cancellation` were deleted from GitHub. A fresh remote-head listing confirmed only `main` remained before the tooling publication branch was pushed.

The operator also explicitly authorized committing, pushing and merging Forge 6.4.3 and the latest documentation. This package contains the 35 installed harness/adapter changes plus the Master Map, Master Plan, project context, changelog and pending audit/runtime documents. A single temporary `chore/forge-context-sync` worktree is bound to main `f4ede895` for independent review; it is due for removal after merge. Merging this tooling/docs package still triggers the unfiltered main image-build and automatic Azure deployment chain, as presented to the operator; no manual broker operation or recovery drill is included. The running research services continue to mount primary. Local receipts and ignored evidence are excluded from the publication.

[Exact-main CI 37179681442](https://github.com/marketsignal/msai-v2/actions/runs/37179681442) passed. [Automatic Deploy 37179711991](https://github.com/marketsignal/msai-v2/actions/runs/37179711991) completed successfully at 05:42 UTC, including release/fleet checks, installation, public frontend/TLS probes and transient SSH deletion. This is workflow evidence at `f4ede895`; the last separate direct VM inventory remains `fa4c8f8`. Actual repaired-runner cancellation and fresh scheduled reaping remain unverified.
