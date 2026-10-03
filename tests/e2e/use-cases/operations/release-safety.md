# Release-safety read-only operator journeys

RS-1 and RS-2 passed preliminary real read-only execution on October 3, 2026. Repeat them at the intended candidate and environment; the historical reference is not evidence for new application revisions. Provider fixtures remain separate regression tests. Actual Azure lifecycle and full deployment acceptance remain pending under RS-3 in the reviewed plan.

## Surface coverage decision

API: covered by RS-2, using the documented authenticated fleet observations.

CLI: covered by RS-1 and RS-2, using the operator release helper and GitHub CLI.

UI: N/A — this capability is release-operator administration through deployment workflows and CLI; it introduces no application UI action or control.

## RS-1 — Choose an evidence-backed release revision

**Actor:** Release operator preparing a research-platform update.

**Scenario:** An image or branch build alone does not establish that the revision can be deployed. The operator needs to distinguish an eligible main revision from an unreleased branch revision.

**Intent:** Select the exact version that has required successful checks, and understand refusal before accessing Azure.

**Interface:** CLI, with real GitHub API responses through the documented GitHub CLI.

**Setup:** Existing authenticated read-only GitHub session; repository `marketsignal/msai-v2`. Observed main reference `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`; branch reference `aa6d44b39f2ad233c49e37161b3f5d50a0d0227d`. No check rerun, workflow dispatch or cloud action.

**Steps:**

1. Run `python3 scripts/release_safety.py check --repository marketsignal/msai-v2 --sha 7f9eb2b5ad252bc3ba730f53a159ca04046278eb --wait-seconds 0`.
2. Inspect the named runs and attempt-specific jobs through `gh api`; confirm the same revision, main/push identity and required successful jobs.
3. Run the same helper for branch reference `aa6d44b39f2ad233c49e37161b3f5d50a0d0227d` and read its refusal. A branch build must not substitute for main-push release evidence.

**Verification:** The successful output identifies the exact requested main SHA and both workflow runs/attempts, corroborated by provider responses. The unreleased branch is refused with missing main-push evidence. No Azure operation occurs.

**Persistence:** Repeat the positive invocation; it must read current evidence for the same SHA rather than accepting the previous output as authority. If provider state changes, record the new result rather than forcing an expected success.

## RS-2 — Inspect the fleet before maintenance

**Actor:** Release operator deciding whether lifecycle state allows a maintenance window.

**Scenario:** The operator must distinguish complete fleet evidence from a capped status list, missing credentials or an older application version.

**Intent:** Obtain a repeatable, authenticated answer about persisted release blockers without trading or altering lifecycle records.

**Interface:** API first, then CLI.

**Setup:** Separately serving candidate API on `http://127.0.0.1:8810`, with startup tasks disabled and broker/vendor access disabled, reading the existing local database. Use the documented development API key. The existing research API on `http://127.0.0.1:8800` remains untouched and represents the old contract. This arrangement proves the read-only API surface, not full startup or production readiness. No direct database setup, stops, starts or resumes.

**Steps:**

1. Request candidate `/health`, authenticated `/api/v1/live/status?active_only=true` and `/api/v1/live/release-readiness`.
2. Read the version, fleet scope, completeness, ready flag and each blocker count. Request readiness without credentials as an access-control check.
3. Invoke `scripts/release_safety.py readiness --base-url http://127.0.0.1:8810` with the documented development key in `MSAI_API_KEY`; compare its decision with the observed response. Do not force a quiet state.
4. Invoke the same helper against the existing `:8800` API. Its missing readiness endpoint must refuse, including when explicit bootstrap is requested for this negative test; HTTP errors are not a fresh-target connection exception.

**Verification:** Authenticated readiness returns a complete typed fleet response; all three counts are nonnegative integers and ready agrees with zero blockers. Missing authentication refuses. The CLI accepts only the observed ready response and otherwise reports the blocker/contract issue. Old API HTTP failure refuses. Readiness is not described as an atomic lock or broker-flat evidence.

**Persistence:** Re-request readiness and repeat the candidate helper invocation. Compare fresh observations; disclose any change. No persistence writes are implied by these read-only checks.
