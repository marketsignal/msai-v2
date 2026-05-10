# Use Cases: Deployment-Pipeline Slice 2 — CI Image Publish

> **Note on classification:** These use cases are **post-merge operator acceptance procedures**, not verify-e2e-regression candidates. The "system under test" is a GitHub Actions workflow file (`.github/workflows/build-and-push.yml`) that can only execute after the file has merged to `main` AND operator-side prerequisites are in place (Bicep AcrPush role assignment applied; 8 GH repo Variables set). The verify-e2e agent cannot exercise these without consuming live Azure resources (ACR storage, GH Actions minutes).
>
> Operators referencing this file are walking through the post-merge smoke that the slicing-verdict ratified as Slice 2's acceptance criterion. The detailed step-by-step procedure lives in `docs/runbooks/vm-setup.md` `## Slice 2 acceptance smoke (10 min)`. This file restates the use cases in the Intent → Steps → Verification → Persistence shape for forward consistency with the rest of `tests/e2e/use-cases/`.
>
> **Source plan:** `docs/plans/2026-05-10-deploy-pipeline-ci-image-publish.md` Phase 5.4 section.

---

## UC-S2-001: Operator triggers `workflow_dispatch` and images appear in ACR

**Interface:** CLI (`gh`, `az`) — no API or UI to test.

**Intent:** Operator manually fires the build workflow and confirms both images land in ACR with correct short-SHA tags.

**Setup (operator-side, one-time):**

- Slice 2 PR merged on `main` (this PR).
- Operator has re-applied Bicep with `./scripts/deploy-azure.sh` (auto-resolves `OPERATOR_IP` via `ifconfig.me`; SSH key from `~/.ssh/{id_ed25519,id_rsa,id_ecdsa}.pub`). The new `ghOidcAcrPushAssignment` resource lands.
- Operator has set all 8 GH repo Variables (per runbook): `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `ACR_NAME`, `ACR_LOGIN_SERVER`, `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_AZURE_CLIENT_ID`, `NEXT_PUBLIC_API_URL`.
- ≥ 60 seconds elapsed since deploy completed (RBAC propagation buffer per research-brief topic 10).

**Steps:**

1. From operator's machine on `main` HEAD: `gh workflow run build-and-push.yml`
2. `gh run watch` and capture the run ID

**Verification:**

1. Run conclusion = `success`: `gh run view <run-id> --json conclusion -q .conclusion` returns `"success"`.
2. `az acr repository show-tags --name <ACR_NAME> --repository msai-backend -o tsv` lists the run's `<sha7>` (first 7 chars of `${{ github.sha }}`).
3. Same check for `msai-frontend`.
4. No `AZURE_CREDENTIALS` / `client_secret` / `ACR_PASSWORD` patterns appear in the workflow run logs.

**Persistence:** Re-running `gh workflow run` on the same `main` HEAD produces the same image digests (cache hits). `az acr manifest list-metadata --name <ACR>:msai-backend:<sha7>` shows the same digest both runs.

---

## UC-S2-002: Push-to-main trigger fires automatically

**Interface:** CLI + git.

**Intent:** A real commit landing on `main` automatically produces published images without operator intervention beyond `git push`.

**Setup:** Same as UC-S2-001.

**Steps:**

1. On `main`: `git commit --allow-empty -m "chore: Slice 2 trigger smoke"; git push origin main`
2. Wait ~10 seconds for GitHub to schedule the run.
3. `gh run list --workflow build-and-push.yml --limit 1`

**Verification:**

1. Most-recent run's `event` column = `push`.
2. Run conclusion = `success`.
3. Images present in ACR with the new commit's `<sha7>`.

**Persistence:** After the run completes, the new tags are immutable in ACR — `az acr repository show-tags` continues to list them.

---

## UC-S2-003: Workflow refuses non-main triggers

**Interface:** CLI.

**Intent:** Defense-in-depth — the workflow should NOT publish images for any ref other than `refs/heads/main`, even if an operator dispatches it from a feature branch via the Actions UI.

**Setup:** Operator on a feature branch (not `main`). All GH Variables already set per UC-S2-001.

**Steps:**

1. `git push origin feature/foo` (a feature branch with no main-merge intent).
2. `gh run list --workflow build-and-push.yml --limit 1` — confirm no new run was triggered for the push event (the `push:` filter restricts to `branches: [main]`).
3. Attempt `gh workflow run build-and-push.yml --ref feature/foo`. The dispatch succeeds in scheduling a run, BUT both jobs are skipped immediately by the workflow-level `if: github.ref == 'refs/heads/main'` guard.

**Verification:**

1. No workflow run created for the `push:` event (filter `branches: [main]` keeps non-main pushes out).
2. The dispatched run on `feature/foo` shows both jobs as `skipped` (not `failure`, not `success`) — `gh run view <run-id>` confirms `conclusion: skipped`.
3. No tags written to ACR for non-main commits (`az acr repository show-tags --name <ACR> --repository msai-backend` does NOT list any feature-branch SHA).

**Persistence:** ACR remains exclusively `main`-tagged.

---

## Out of scope for Slice 2 acceptance

- **Image runtime correctness** (does the backend image start? does the frontend serve?) — Slice 3 verifies via `up -d --wait` + `/health` + `/ready` curls. Slice 2 only proves images are _publishable_.
- **DNS / reverse proxy / TLS** — Slice 3+.
- **Image scanning / signature verification** — Slice 4 ops if at all.
- **Multi-arch (linux/arm64) builds** — eastus2 VM is x86_64; explicit non-goal.
- **Image-retention / pruning** — Slice 4 ops; ACR retains all SHAs until then.

## Failure-mode reference

See `docs/runbooks/vm-setup.md` `## Slice 2 acceptance smoke (10 min) → ### Slice 2 — If something fails` for the 5 documented failure modes (RBAC propagation 403; missing GH Variable; AADSTS70021 federated-credential rejection; cache-scope misconfig; concurrency cancellation behavior).
