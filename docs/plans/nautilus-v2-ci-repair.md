# PR112 RC6 CI repair

This is a bounded follow-up to the approved V2 research plan and human request “good, repair then and continue.” Continue existing feat/nautilus-v2-research/PR112; immutable workflow base9d51a633ebeeff9b85ee06b6085df056dbe68458, pre-repair published HEAD921acbb73dad5fca93ddaed8c0e60169135cc00f. No merge, deployment, broker/vendor operation or cleanup.

## Proven failures and native boundary

Remote CI37482504106/backend112334006489 and local canonical backend-cwd `python -m mypy src/ --strict` both report11 errors in225 files before edits. Four files retain V1 live definitions whose modules/config exports are absent in exact2.0.0rc6. Two legacy ActorConfig classes become Any/frozen errors; three legacy ignores are unused. Two owning legacy tests collected locally fail before app imports because model.enums/cache.cache no longer exist. Thus the old full pytest invocation has an additional reachable collection failure.

Independent research-first confirmed this against pinned RC6 wheel/source and official migration guide/release. V2 native DataActorConfig/LiveNodeConfig have different contracts; replacing legacy names would falsely imply migrated live execution. The approved product remains isolated synthetic-equity research, preserving immutable1.223 recovery and explicitly refusing unsupported live operations.

## Alternatives and selected fix

1. Keep V1 live implementation intact behind explicit runtime refusal, confine typing accommodations to its unavailable import boundary, and declare the actual supported RC6 positive research suite. Preserve full-source Ruff/strictmypy, release-control checks and frontend/image gates. This fixes the reachable supported CI contract without inventing V2 live compatibility.
2. Migrate all live execution/cache/event/actor contracts and legacy test matrix to V2. This is outside the approved research acceptance and cannot be certified by a typing fix. Reject this expansion here.

Select1. No blanket module ignore_errors, missing error suppression for supported research, global no-warn-unused-ignores, negative skip-list or source-only fake V1 test.

## Ownership and implementation

One forge-v6-producer owns four legacy sources: `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`, `live_node_config.py`, `data_freshness_actor.py`, and `backend/src/msai/services/symbology_shim_actor.py`; CI `.github/workflows/ci.yml`; a small Python CI suite selector/positive manifest under `scripts/`; meaningful owning regression tests under backend/tests/unit; and bounded explanation additions to docs/runbooks/nautilus-v2-research.md/docs/CHANGELOG.md. Existing runtime capability helper may be reused; change it only if a proven refusal boundary requires it. No other product behavior or dependency pin changes.

Guard unsupported actor/config execution before absent native imports or legacy construction, while retaining native-independent helpers and cold-reader callsites. Actor configs may use their real V1 msgspec.Struct static inheritance contract under TYPE_CHECKING with runtime actual V1 ActorConfig, verifying actual MRO/serialization. Keep live builders on actual V1 configs when1.223 is executed; defer imports if needed and use narrowly explained type ignores only for unavailable V1 exports. Remove obsolete ignores only after real1.223 owning controls establish no lost compatibility. Do not alias V2 configs into V1 code.

Add exactRC6 suite selection based on actual installed engine metadata. Manifest includes the22 certified owning files from final-verify-app.prompt.md, plus new refusal/selector regression tests. Check manifest nonempty/unique/existing paths and exact2.0.0rc6; refuse unknown2.x versions. Print research-only scope and explicitly unexecuted legacy live/full-suite acceptance. Preserve the ordinary full tests/ route for1.x as an unverified compatibility path, without claiming the current V2 source works on1.x. Keep full-source Ruff/mypy and all other existing CI gates unchanged. Normal pytest/cov options remain applicable.

## RED → GREEN controls

Before production edits, write and observe meaningful new regressions: RC6 legacy live entry points refuse actionably before unavailable native import/I/O; supported independent date/helper import remains usable. Selector rejects unknown engine, missing/duplicate/empty paths and produces actual nonempty supported research selection. The already-observed exact full strict-mypy RED is the typing acceptance regression; do not add a mirrored comments test.

After minimal repair: full `mypy src/ --strict` and full `ruff check src/` from backend; owning new tests plus supported research manifest. No exhaustive local test run. Independently execute actual1.223 in a separate environment or immutable image with only the repaired legacy modules/helper/tests overlaid; test native config inheritance, serialization/constructor compatibility and retained refusal semantics. Record exact revision/source/env and limited scope; this is not candidate V1 application/live acceptance. Preserve existing images/volumes and previous reports.

## Final acceptance and publication

One fresh independent plan review challenges this scope before implementation. Producer emits genuine task spec/quality receipts with actual runtime identity. Finish docs/simplification, stage/freeze new exact candidate and increment current workflow review iteration monotonically. Fresh paired reviews focus this named CI/refusal repair plus direct regressions while preserving earlier whole-feature findings. Fresh verify-app checks full-source strict typing/lint and supported research/repair selection; fresh complete5UC final API→CLI→real-browser research/persistence/refusal/actual1.223 recovery matrix uses new candidate Linux image. Preserve baseline/data/DB/Redis; root updates isolated five app services only after public-job preflight. No frontend source changes; use unchanged accepted frontend build and actual browser functionality, no claim of a new frontend build if not executed.

Promote only certified exact tree. Human request repairs and continues this existing PR; present concrete resulting candidate/diff and execute any already-authorized bounded PR update through normal feature-cwd shipping hooks, without force or merge. Check exact remote head and remote CI; reachable failures receive focused diagnosis/repair under the bounded workflow. Keep actual failed/partial evidence and no relabeled receipts.
