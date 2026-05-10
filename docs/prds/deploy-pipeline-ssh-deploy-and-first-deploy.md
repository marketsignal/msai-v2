# PRD: Deployment Pipeline Slice 3 — SSH Deploy + First Real Production Deploy

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-10
**Last Updated:** 2026-05-10

---

## 1. Overview

Slice 3 of the 4-PR deployment-pipeline series, ratified at [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md). Ships the deploy job that consumes Slice 2's ACR images: SSH from a GH-hosted runner to the prod VM, `docker compose pull` the immutable `<sha7>` tag pair, run the one-shot `migrate` service, bring the rest of the stack up with `--wait`, run a deploy-success probe (`/health` + `/ready` + `XINFO GROUPS msai:live:commands`), and on probe failure roll back to the last-good SHA. Adds a Caddy reverse-proxy in front of backend + frontend on `platform.marketsignal.ai` with automatic Let's Encrypt TLS. Enables Slice 1's `msai-render-env.service` so `/run/msai.env` is materialized from Key Vault on every boot.

**The first real production deploy lives here.** Per the council slicing verdict §Slice 3, this slice cannot merge until two blocking gates are honored:

- **Hawk's gate:** `scripts/backup-to-blob.sh` runs against the empty prod Postgres and the dump appears in the `msai-backups` Blob container — BEFORE the first `up -d --wait`.
- **Contrarian's gate:** the full VM deploy path is rehearsed end-to-end in a throwaway resource group before merge.

## 2. Goals & Success Metrics

### Goals

- **One-command production deploy.** A push to `main` (or manual `workflow_dispatch`) deploys the corresponding `<sha7>` image pair to the prod VM with no operator hand-holding beyond the two blocking gates above.
- **Deterministic, reversible image references.** Deploys pin `MSAI_GIT_SHA=<sha7>` from the workflow context; rollback is `gh workflow run deploy.yml -f git_sha=<previous-sha7>` with no compose edits.
- **Fail-fast probe before traffic flows.** `up -d --wait` on its own only proves containers entered `running` state; the probe verifies `/health` (200), `/ready` (200), and the `msai:live:commands` Redis stream's consumer group exists. Any probe failure triggers automatic rollback to the last-good SHA before the deploy job exits non-zero.
- **TLS without operator clicks.** Caddy 2 with automatic Let's Encrypt issuance terminates HTTPS at `platform.marketsignal.ai`, reverse-proxies `/api/*` to backend:8000 and `/*` to frontend:3000. Cert renewal is automatic; cert state is persisted to a named Docker volume.
- **Active-live-trading guard (deferred to Slice 4).** Slice 3 declares but does NOT enforce the `live_deployments`-active hard gate per slicing verdict (Slice 4 ships the gate). Slice 3 deploys via the default profile only — the `broker` profile (live-supervisor + ib-gateway) is explicitly excluded.

### Success Metrics

| Metric                                       | Target                                                                                                                                                                                                                              | How Measured                                                                                                                                                                                                       |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Workflow green on push-to-main               | `deploy.yml` run completes with `success` conclusion                                                                                                                                                                                | `gh run list --workflow=deploy.yml --limit 1 --json conclusion -q '.[0].conclusion'` returns `success`                                                                                                             |
| Backend reachable via Caddy                  | `curl https://platform.marketsignal.ai/health` returns 200                                                                                                                                                                          | Operator + workflow probe                                                                                                                                                                                          |
| Frontend reachable via Caddy                 | `curl -I https://platform.marketsignal.ai/` returns 200 with `content-type: text/html`                                                                                                                                              | Operator + workflow probe                                                                                                                                                                                          |
| TLS cert issued                              | Cert chain leads to ISRG Root X1 (Let's Encrypt)                                                                                                                                                                                    | `openssl s_client -connect platform.marketsignal.ai:443 -servername platform.marketsignal.ai </dev/null \| openssl x509 -noout -issuer` shows `O=Let's Encrypt`                                                    |
| Deploy success signal — three probes pass    | `/health` 200 + `/ready` 200 + `XINFO GROUPS msai:live:commands` returns at least the `live-supervisor` consumer group (NB: only checkable when broker profile up; Slice 3 deploys default profile so this probe degrades — see §6) | Workflow log shows three explicit probe steps; the `XINFO` probe is `--soft-fail` on the default profile (logged but non-blocking) and `--hard-fail` on the broker profile (Slice 4 enforces broker-active gating) |
| Rollback on probe failure                    | If any HARD-fail probe fails, workflow re-runs `docker compose pull && up -d --wait` with the last-good SHA, then exits 1                                                                                                           | Rehearsal smoke: introduce a deliberate breakage, observe rollback, observe non-zero exit                                                                                                                          |
| Hawk's gate — backup verifiable in Blob      | `az storage blob list --container msai-backups --prefix backup-` returns ≥1 blob whose latest segment matches today                                                                                                                 | Operator runs script, then `az storage blob list`; result captured in PR description                                                                                                                               |
| Contrarian's gate — throwaway-RG smoke clean | Full deploy pipeline runs end-to-end against a `msaiv2-rehearsal-<date>` RG, RG torn down                                                                                                                                           | Operator captures `gh run` URL + final cleanup `az group delete` confirmation; pasted into PR description                                                                                                          |
| Acceptance smoke E2E time                    | < 8 min per deploy (cold caches), < 4 min steady-state                                                                                                                                                                              | Workflow run duration                                                                                                                                                                                              |

### Non-Goals (Explicitly Out of Scope — picked up by Slice 4 or later)

- ❌ Active-live-trading hard gate (`live_deployments`-active refusal) — Slice 4
- ❌ Nightly cron backups + DR alert rules — Slice 4
- ❌ Image retention/pruning policy in ACR — Slice 4
- ❌ Image scanning (Trivy/Snyk) — Slice 4 or post-Phase-1
- ❌ Multi-environment promotion (dev → staging → prod) — Phase 2
- ❌ Trying to deploy the `broker` profile (live-supervisor + ib-gateway) — explicit exclusion per architecture verdict §Blocking Objection #7. Operators bring up broker stack manually outside CI for now.
- ❌ Frontend Entra SPA app registration (using backend `AZURE_CLIENT_ID` temporarily; tracked as deferred follow-up — see §10)
- ❌ Postgres logical replication / Flexible Server migration — Phase 2
- ❌ DNS automation (operator points the A record manually before first deploy)

## 3. User Personas

### Pablo (operator)

- **Role:** Solo operator. Owns the codebase + Azure subscription + DNS.
- **Permissions:** Subscription Owner on MarketSignal2; admin on `marketsignal/msai-v2`; DNS admin on `marketsignal.ai`.
- **Pre-Slice-3 prep (Pablo confirms before merge):**
  - DNS: `platform.marketsignal.ai` A record → `vmPublicIp` from Slice 1 Bicep outputs (TTL ≤ 300s).
  - 7 GitHub repo Variables already set from Slice 2 (`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `ACR_NAME`, `ACR_LOGIN_SERVER`, `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_API_URL`). Slice 3 adds: `MSAI_HOSTNAME=platform.marketsignal.ai`, `MSAI_REGISTRY` (= `ACR_LOGIN_SERVER`), `MSAI_BACKEND_IMAGE=msai-backend`, `MSAI_FRONTEND_IMAGE=msai-frontend`, `VM_PUBLIC_IP`, `VM_SSH_USER=msaiadmin`, `RESOURCE_GROUP=msaiv2_rg`, `KV_NAME` (from Slice 1 outputs). Slice 3 also requires GitHub Secret `VM_SSH_PRIVATE_KEY` (operator-owned ed25519 keypair; public half lives in Bicep).
  - Slice 1 KV is seeded with all 8 REQUIRED + 5 OPTIONAL secrets per `scripts/msai-render-env.service`. Pablo confirmed this happens during Slice 1 acceptance — re-verify before first Slice 3 deploy.
  - **Hawk's gate:** runs `scripts/backup-to-blob.sh` against empty prod Postgres + verifies blob; pastes evidence in PR.
  - **Contrarian's gate:** runs full deploy pipeline against a throwaway RG with `--rehearsal` flag (provisions RG → Slice 1 deploys → Slice 2 image push triggered manually → Slice 3 deploy.yml dispatched against rehearsal RG → tear down). Pastes evidence in PR.
  - **First real prod deploy:** triggers `gh workflow run deploy.yml` after merge; observes 5/5 acceptance smoke (TLS chain, `/health` 200, `/ready` 200, `XINFO GROUPS` returns groups, Caddy access log shows the request).

### GitHub Actions deploy job (`.github/workflows/deploy.yml`)

- **Role:** Stateless deploy runtime that, on push-to-main (via `workflow_run` after Slice 2's build succeeds) or `workflow_dispatch`, exchanges OIDC for an Azure access token, opens a transient NSG rule for the runner's IP (council Plan-Review iter-1 P0 — see `docs/decisions/deploy-ssh-jit.md`), SSHs to the VM, runs `scripts/deploy-on-vm.sh` (which authenticates to ACR via the VM's own system-assigned MI — no token transit through CI per research §6), and probes for public-internet success.
- **Permissions:** `id-token: write`, `contents: read`. SSH private key from `${{ secrets.VM_SSH_PRIVATE_KEY }}`. AcrPull is on the VM's system-assigned MI (Slice 1).

### `scripts/deploy-on-vm.sh` (the per-deploy script that runs ON the VM)

- **Role:** Runs as `root` (sudo) on the VM during the SSH session. Pulls the new images, captures the previous SHA from `/run/msai-images.env` for rollback, restarts `msai-render-env.service` (re-fetches secrets in case Slice 1 KV was rotated), runs `docker compose pull && up -d --wait migrate backend backtest-worker research-worker portfolio-worker ingest-worker frontend caddy`, runs the success-probe trio, and on failure re-pins to the last-good SHA and re-runs `up -d --wait` before exiting non-zero.
- **Permissions:** `root` via sudo (cloud-init grants `msaiadmin` passwordless `sudo`). System-assigned MI for ACR pull.

## 4. User Stories

### US-001: Push-to-main deploys to prod

**As an** operator (Pablo)
**I want** every push to `main` (post-Slice-2 image build) to deploy automatically to the prod VM
**So that** I never have to SSH manually for routine deploys

**Scenario:**

```gherkin
Given Slice 1 IaC is applied to msaiv2_rg (VM running, Caddy NSG 80/443 inbound rules in place — verified Slice 1)
And Slice 2's build-and-push.yml has produced msai-backend:abc1234 and msai-frontend:abc1234 in ACR
And the operator has set the 8 new repo Variables and 1 new repo Secret per §3
And DNS A record platform.marketsignal.ai → VM public IP is propagated
And Hawk's gate has been honored (backup verified in Blob)
And Contrarian's gate has been honored (rehearsal RG smoked clean and torn down)
When I push commit abc1234 to main (or run `gh workflow run deploy.yml -f git_sha=abc1234`)
Then GitHub Actions runs .github/workflows/deploy.yml after build-and-push.yml succeeds
And deploy.yml exchanges an OIDC token, mints a one-shot ACR access token, SSHs to the VM
And deploy-on-vm.sh records the previous /run/msai-images.env (last-good SHA) into /run/msai-images.last-good.env
And deploy-on-vm.sh writes /run/msai-images.env with MSAI_GIT_SHA=abc1234
And deploy-on-vm.sh restarts msai-render-env.service (refresh /run/msai.env from KV)
And deploy-on-vm.sh runs `docker compose -f docker-compose.prod.yml --env-file /run/msai.env --env-file /run/msai-images.env pull` against ACR
And deploy-on-vm.sh runs `docker compose ... up -d --wait --wait-timeout 300 migrate backend backtest-worker research-worker portfolio-worker ingest-worker frontend caddy`
And deploy-on-vm.sh probes /health (200) + /ready (200) on backend
And deploy-on-vm.sh probes `redis-cli XINFO GROUPS msai:live:commands` (soft-fail on default profile — see §6)
And deploy-on-vm.sh probes `curl -sf https://platform.marketsignal.ai/health` (Caddy + LE end-to-end)
And the workflow run concludes with success
```

**Acceptance Criteria:**

- [ ] `.github/workflows/deploy.yml` exists, triggers on `workflow_run: workflows: ["Build and Push Images"], types: [completed], branches: [main]` AND `workflow_dispatch` with optional `git_sha` input
- [ ] Deploy runs only when the prerequisite `Build and Push Images` workflow concluded `success` (workflow_run filter)
- [ ] Workflow has `permissions: id-token: write, contents: read`
- [ ] Workflow uses `azure/login@v2` with same OIDC pattern as Slice 2 — no client secret
- [ ] Workflow installs the SSH key from `${{ secrets.VM_SSH_PRIVATE_KEY }}` into a runner-only `~/.ssh/` (cleaned up by `actions/setup-ssh@v…` or inline `eval $(ssh-agent)` pattern); `StrictHostKeyChecking=yes` with the VM's host key pinned via `${{ vars.VM_SSH_KNOWN_HOSTS }}` (initial value captured during Slice 1 acceptance)
- [ ] Workflow `scp`s `scripts/deploy-on-vm.sh` to the VM at `/tmp/deploy-on-vm-<run-id>.sh`, chmods +x, executes via SSH, and removes after run regardless of exit code
- [ ] `scripts/deploy-on-vm.sh` exists, idempotent, exits non-zero with a single-line classified failure marker (`FAIL_PULL`, `FAIL_MIGRATE`, `FAIL_PROBE_HEALTH`, `FAIL_PROBE_READY`, `FAIL_PROBE_TLS`, `FAIL_ROLLBACK`) for log grep
- [ ] On any `FAIL_PROBE_*`, deploy-on-vm.sh restores `/run/msai-images.env` from `/run/msai-images.last-good.env`, runs `docker compose ... up -d --wait` again, and exits with `FAIL_ROLLBACK_OK` (rollback succeeded; deploy failed) or `FAIL_ROLLBACK_BROKEN` (rollback also failed; manual intervention)
- [ ] `concurrency: group: deploy-msai, cancel-in-progress: false` (do NOT cancel in-flight deploys — let them finish or fail safely; the next deploy waits)
- [ ] Workflow run summary embeds the final probe outputs (curl `-i`, `redis-cli XINFO GROUPS`, openssl s_client cert chain) for audit
- [ ] No secret-typed value other than `VM_SSH_PRIVATE_KEY` is referenced; ACR access token is minted in-workflow, masked, and discarded

**Edge Cases:**

| Condition                                                        | Expected Behavior                                                                                                                                                     |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Build workflow failed                                            | `workflow_run` filter prevents deploy from running                                                                                                                    |
| `workflow_dispatch` with explicit `git_sha`                      | Skips workflow_run filter; deploys whatever `git_sha` is passed (no validation that the image tag exists — Docker pull will fail fast if it doesn't)                  |
| Two pushes land on `main` within seconds                         | Both build workflows run; both deploy workflows queue; concurrency group serializes deploys (cancel-in-progress: false → newer waits, never cancels in-flight)        |
| Migrate service exits non-zero                                   | `up -d --wait` returns failure; `FAIL_MIGRATE`; rollback runs; deploy job exits non-zero                                                                              |
| Backend probe `/health` returns 502 (Caddy up but backend dying) | `FAIL_PROBE_HEALTH`; rollback runs                                                                                                                                    |
| TLS probe fails (Caddy can't reach LE)                           | `FAIL_PROBE_TLS`; rollback runs. Common causes: NSG 443 misconfig, DNS A record drift, LE rate limit                                                                  |
| VM SSH unreachable                                               | Workflow fails fast at SSH step; no rollback needed (no state changed)                                                                                                |
| VM disk full                                                     | `docker compose pull` fails; `FAIL_PULL`; no state changed; deploy fails fast                                                                                         |
| KV secret rotated mid-deploy                                     | `msai-render-env.service` runs at top of deploy-on-vm.sh; new secrets land in `/run/msai.env`; compose `--env-file /run/msai.env` picks them up                       |
| Active live-trading session in progress (`broker` profile up)    | Slice 3 explicitly does NOT touch broker profile services; `up -d --wait <service-list>` enumerates only default-profile services. Slice 4 adds the hard refusal gate |
| Same SHA re-deployed (idempotent re-run)                         | Compose pulls (cached → no-op), re-runs migrate (alembic-head idempotent), restarts containers if image digest changed (no-op if unchanged), probes succeed           |
| Caddy can't acquire LE cert on first run (DNS not propagated)    | Caddy crashloops; `FAIL_PROBE_TLS`; rollback runs but the rollback ALSO fails (Caddy state independent of SHA). Operator must fix DNS, then re-deploy                 |

**Priority:** Must Have

---

### US-002: Operator manually rolls back to a previous SHA

**As an** operator (Pablo)
**I want** to roll back the prod stack to a known-good prior SHA without editing files
**So that** I can recover from a bad deploy in <2 minutes

**Scenario:**

```gherkin
Given the current prod deploy is msai-backend:abc1234 / msai-frontend:abc1234 and is misbehaving
And the previous-good SHA was zzz9999
And ACR retains zzz9999 (default retention: last 5 SHAs per slicing verdict)
When I run `gh workflow run deploy.yml -f git_sha=zzz9999`
Then deploy.yml deploys zzz9999 by the same code path as a normal deploy
And the probes pass against the rolled-back stack
```

**Acceptance Criteria:**

- [ ] `workflow_dispatch` declares an optional `git_sha` input; default = workflow's commit SHA short
- [ ] If `git_sha` is provided, deploy uses it; otherwise uses the workflow's own commit
- [ ] Validation: input is exactly 7 hex characters (regex `^[0-9a-f]{7}$`); otherwise the workflow fails fast with a clear error before SSHing

**Priority:** Must Have

---

### US-003: Public web traffic over HTTPS via Caddy + Let's Encrypt

**As a** end user
**I want** to load `https://platform.marketsignal.ai` and see a valid TLS cert
**So that** my browser doesn't warn me and Entra ID redirect URIs work

**Scenario:**

```gherkin
Given DNS A record platform.marketsignal.ai → VM public IP has propagated
And NSG allows 80 + 443 inbound from Internet (Slice 1 — already in place)
And the Caddy compose service is running with persistent /data and /config volumes
When I `curl -I https://platform.marketsignal.ai/health`
Then the TLS handshake completes with a Let's Encrypt-issued cert
And the response is 200 OK from backend
And the cert is automatically renewed when within 30 days of expiry (Caddy default)
```

**Acceptance Criteria:**

- [ ] `docker-compose.prod.yml` adds a `caddy` service: image `caddy:2-alpine`, ports `80:80` and `443:443` published to host, `caddy_data` and `caddy_config` named volumes mounted at `/data` and `/config`
- [ ] `caddy` `depends_on: backend: service_healthy` AND `frontend: service_started` (frontend has no healthcheck today; service_started is acceptable — cert issuance does not depend on frontend readiness)
- [ ] `Caddyfile` at repo root is mounted read-only into the caddy container at `/etc/caddy/Caddyfile`
- [ ] Caddyfile reverse-proxies `/api/*` → `backend:8000`, all other paths → `frontend:3000`, with hostname `{$MSAI_HOSTNAME}` (interpolated from env)
- [ ] Caddy auto-issues LE cert on first start; cert state persists in `caddy_data` named volume across restarts
- [ ] Cloud-init does NOT need updates for Caddy (it lives in compose, not on the host)
- [ ] Backend's `CORS_ORIGINS` KV secret value is updated by operator to `["https://platform.marketsignal.ai"]` BEFORE first deploy (operator action, surfaced in §10)
- [ ] Frontend's `NEXT_PUBLIC_API_URL` repo Variable is set to `https://platform.marketsignal.ai/api/v1` (or just the bare host — confirm in research, see §11) BEFORE Slice 2's image build picks it up — i.e. operator updates this Variable, then triggers `gh workflow run build-and-push.yml` to rebuild the frontend image, then the Slice 3 deploy picks up the new image

**Edge Cases:**

| Condition                                   | Expected Behavior                                                                                                                                                                                                                                       |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| LE rate-limit hit (5 certs / domain / week) | Caddy logs the rate-limit error; request serves stale cert if any; if first deploy, the deploy fails. Mitigation: rehearsal RG must use a separate hostname (`platform-rehearsal.marketsignal.ai`) — see §11.                                           |
| Caddyfile syntax error                      | Caddy crashloops; FAIL_PROBE_TLS; rollback restores last-good Caddyfile. Caddy validates Caddyfile at container start; deploy-on-vm.sh runs `docker compose run --rm caddy caddy validate --config /etc/caddy/Caddyfile` BEFORE the main `up -d --wait` |
| Cert renewal fails (LE outage)              | Caddy serves the existing cert until expiry; emits log warnings; cert renewal retries with backoff. No deploy action needed unless cert is within hours of expiry                                                                                       |
| Operator changes `MSAI_HOSTNAME`            | Caddy will attempt to issue a new cert for the new hostname. If DNS doesn't resolve, fails. Runbook: update DNS first, then `MSAI_HOSTNAME` repo Variable, then redeploy                                                                                |

**Priority:** Must Have

---

### US-004: Hawk's gate — backup verified before first deploy

**As an** operator (Pablo)
**I want** to prove that `scripts/backup-to-blob.sh` works against the empty prod Postgres
**So that** I have a backup workflow before any data could be at risk

**Scenario:**

```gherkin
Given Slice 1 IaC is applied; msaiv2_rg has the storage account and msai-backups container
And the prod VM is running but no app is deployed yet (or the app was deployed once with an empty DB)
When I run `scripts/backup-to-blob.sh` (updated in this slice to read storage account name from Bicep outputs)
Then the script dumps the (empty) Postgres, copies the (empty) Parquet tree, and uploads to msai-backups
And `az storage blob list --account-name <bicep-output> --container-name msai-backups --prefix backup-` returns ≥1 blob with today's date
```

**Acceptance Criteria:**

- [ ] `scripts/backup-to-blob.sh` updated:
  - Reads target storage account name from `az deployment group show --name <last-deploy> --resource-group msaiv2_rg --query 'properties.outputs.backupsStorageAccount.value' -o tsv` instead of hardcoded `msaistorage`
  - Reads container name from `backupsContainerName` Bicep output
  - Authenticates via the VM's system-assigned MI (already has Storage Blob Data Contributor on the storage account per Slice 1)
  - `set -euo pipefail` already in place; add explicit error message + exit code on missing Bicep outputs (script is meant to run on the VM; if `az` CLI isn't logged in via MI, fail with a remediation hint)
- [ ] Hawk's evidence appears in the PR description: terminal capture of script run + `az storage blob list` output showing the dump

**Priority:** Must Have (blocking)

---

### US-005: Contrarian's gate — rehearsal in throwaway RG

**As an** operator (Pablo)
**I want** to prove the full deploy pipeline works end-to-end without touching prod
**So that** the first real prod deploy is the second time the path runs

**Scenario:**

```gherkin
Given a throwaway resource group `msaiv2-rehearsal-<YYYYMMDD>` exists in MarketSignal2 subscription
When the operator follows `docs/runbooks/slice-3-rehearsal.md` (NEW in this slice):
  1. Provision throwaway RG via `infra/main.bicep` with rehearsal-only parameters (separate hostname `platform-rehearsal.marketsignal.ai`, separate KV name, etc.)
  2. Seed KV with rehearsal secrets (paper-only IB account; smaller LE cert footprint)
  3. Trigger `gh workflow run build-and-push.yml` (Slice 2)
  4. Trigger `gh workflow run deploy.yml -f git_sha=<rehearsal-sha7>` with rehearsal-RG-targeting via a one-off `RESOURCE_GROUP` workflow input
  5. Verify all 5 acceptance probes pass against the rehearsal stack
  6. `az group delete --name msaiv2-rehearsal-<date> --yes`
Then the rehearsal evidence is captured in the PR description (gh run URL + final delete confirmation)
```

**Acceptance Criteria:**

- [ ] `docs/runbooks/slice-3-rehearsal.md` exists with the 6-step procedure above
- [ ] `deploy.yml` accepts a `resource_group` workflow_dispatch input (default = `msaiv2_rg`); rehearsal uses `msaiv2-rehearsal-<date>`
- [ ] `infra/main.bicepparam` documents how to override `vmName`, `kvName`, etc. for a rehearsal RG (or the rehearsal runbook spells out the explicit `--parameters` overrides)
- [ ] Rehearsal evidence in PR description: `gh run` URL + cleanup confirmation

**Priority:** Must Have (blocking)

---

### US-006: First real production deploy

**As an** operator (Pablo)
**I want** to run the first real prod deploy after the PR merges
**So that** Slice 3 ships with empirical proof of the path

**Scenario:**

```gherkin
Given the PR has merged to main (CI green, both gates honored)
And build-and-push.yml has run and produced abc1234 images in ACR
When I run `gh workflow run deploy.yml -f git_sha=abc1234` (or wait for the auto-trigger)
Then deploy.yml runs against msaiv2_rg (default RESOURCE_GROUP)
And all 5 acceptance probes pass:
  1. /health returns 200 (curl from runner)
  2. /ready returns 200
  3. XINFO GROUPS msai:live:commands returns at least the empty stream + group metadata (soft-fail on default profile)
  4. https://platform.marketsignal.ai/ returns 200 with valid LE cert chain
  5. Caddy access log shows the probe requests landing
And evidence is captured in CHANGELOG.md as Slice 3 acceptance
```

**Acceptance Criteria:**

- [ ] Acceptance smoke evidence in `docs/CHANGELOG.md` Slice 3 entry
- [ ] `state.md` Done section logs the prod-deploy SHA + run URL

**Priority:** Must Have (operator gate, post-merge)

---

## 5. Constraints & Assumptions

### Hard Constraints (architectural verdict)

- Single-VM Phase 1 deploy via `docker compose -f docker-compose.prod.yml`
- ACR for image registry (already in Slice 2)
- KV + system-assigned MI for secrets (already in Slice 1)
- Forward-only migrations; DB downgrades not supported
- `live_deployments`-active hard gate is Slice 4, not Slice 3 (slicing verdict)
- No GHCR; no managed Postgres; no AKS yet

### Hard Constraints (Slice 3-specific)

- Caddy in compose, not as systemd unit (simpler; one source of truth for service lifecycle)
- LE issuance happens at first deploy on prod; rehearsal MUST use a different hostname to avoid LE rate limits (5/domain/week)
- Frontend uses backend's `AZURE_CLIENT_ID` for SPA auth temporarily (operator decision recorded in §3)
- The `--wait-timeout 300` cap on `up -d --wait` accommodates IB Gateway's `start_period: 180s` IF the broker profile is ever included; default profile has only Postgres/migrate/backend dependencies and converges in <120s under normal cold start (verified during Slice 2's local smoke against published images)

### Assumptions (verify in Phase 2 research)

- `appleboy/ssh-action` (or raw `ssh -i ...`) is the recommended pattern for SSH-from-runner in 2026; revisit if a current best-practice action exists
- `docker compose --env-file <a> --env-file <b>` merges in declaration order (later wins); confirm with current docs
- `docker compose pull` in 2026 docker-compose-plugin v2.40+ supports authenticated ACR pulls via `~/.docker/config.json` (set up via `docker login` once, persisted on VM); confirm
- Caddy 2 in 2026 still uses Caddyfile + automatic HTTPS by default; confirm config syntax stable
- LE rate limit policies unchanged
- Azure `az storage blob` CLI verbs unchanged

## 6. Open Decisions (resolved during Phase 2/3)

### D-1: How do we probe `XINFO GROUPS msai:live:commands` when broker profile is down?

The architecture verdict §7 (Observability + rollback) declares the success signal as `/health` + `/ready` + `XINFO GROUPS msai:live:commands`. But the live-supervisor (which creates the `live-supervisor` consumer group on the `msai:live:commands` stream) only runs under the `broker` profile, which Slice 3 does NOT bring up.

**Resolution:** Slice 3 probes `XINFO GROUPS msai:live:commands` as a SOFT probe — log the result, don't block on it. Slice 4 (when the broker-active hard gate is added) adds a HARD `XINFO GROUPS` probe that fires only when the deploy actually started/restarted the broker profile. This matches the verdict's intent (verify the live-trading control plane exists before declaring success) without forcing every deploy to spin up IB Gateway.

### D-2: Caddyfile under git or rendered from a template?

The hostname `platform.marketsignal.ai` is a known constant (operator-declared). No template needed. Caddyfile is a small static file checked in at repo root, mounted into the Caddy container.

### D-3: Acceptance probes — run from the GH runner, the VM, or both?

Both. The VM probe (in `deploy-on-vm.sh`) catches local-network issues before the runner-side probe; the runner probe (in `deploy.yml`) confirms public-internet reachability through Caddy + LE.

### D-4: TLS probe target — backend `/health` (proxied by Caddy) or Caddy's own root path?

Backend `/health` proxied through Caddy (`https://platform.marketsignal.ai/health`). Tests TLS handshake AND backend responsiveness AND Caddy proxy config in one probe.

## 7. Dependencies

- **Slice 1 (merged):** Bicep IaC, ACR, KV, VM with system-assigned MI, NSG with 80/443 inbound, render-env-from-kv.sh + msai-render-env.service planted via cloud-init (NOT enabled — Slice 3 enables)
- **Slice 2 (merged):** build-and-push.yml producing `msai-backend:<sha7>` and `msai-frontend:<sha7>` in ACR
- **External — operator:** DNS A record for `platform.marketsignal.ai` (operator action before first deploy)
- **External — operator:** KV seeded with all 8 REQUIRED + 5 OPTIONAL secrets per `scripts/msai-render-env.service`; specifically, `CORS_ORIGINS` updated to `["https://platform.marketsignal.ai"]`
- **External — operator:** SSH keypair generated, public half placed in Bicep, private half added as GitHub Secret `VM_SSH_PRIVATE_KEY`

## 8. Discovery Notes (recorded 2026-05-10)

| Question                            | Decision                                                                                                                |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Reverse-proxy / DNS                 | **Caddy on VM + Let's Encrypt** (recommended option). Single container, automatic TLS, simplest operations.             |
| Frontend deploy scope               | **Backend + frontend together.** Slice 3 ships the full stack to prod.                                                  |
| Hostname                            | **`platform.marketsignal.ai`**                                                                                          |
| Frontend Entra SPA app registration | **Reuse backend `AZURE_CLIENT_ID` temporarily.** Acceptable for Slice 3; replace with dedicated SPA reg in a follow-up. |

## 9. Risks & Mitigations

| Risk                                                             | Likelihood | Impact | Mitigation                                                                                                                                                                                                     |
| ---------------------------------------------------------------- | ---------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| LE rate-limited during rehearsal then can't issue prod cert      | M          | H      | Rehearsal uses `platform-rehearsal.marketsignal.ai` (different hostname → different LE rate-limit bucket)                                                                                                      |
| SSH-from-runner pattern stale (action deprecated)                | M          | M      | Phase 2 research confirms current best practice; if `appleboy/ssh-action` is deprecated, use raw `ssh` with `ssh-agent`                                                                                        |
| Caddy can't reach LE on first try (cert acquisition timeout)     | L          | M      | `scripts/deploy-on-vm.sh` allows up to 120s for Caddy first start before probing `/health` over TLS; if probe fails, rolls back AND surfaces a remediation hint (DNS/NSG/LE check sequence) in the failure log |
| `docker login` to ACR via short-lived token expires mid-deploy   | L          | M      | Token has 3-hour lifetime; deploys complete in <8 min; no realistic exposure                                                                                                                                   |
| KV secret rotation mid-deploy leaves backend with stale env      | L          | M      | `msai-render-env.service` is restarted at top of every deploy-on-vm.sh — secrets refreshed before compose pulls; if rotation happens between render and `up -d`, next deploy picks it up                       |
| First prod deploy reveals an unforeseen integration gap          | M          | H      | Contrarian's gate — full rehearsal in throwaway RG before merge — surfaces these gaps without touching prod                                                                                                    |
| Frontend reuse of backend `AZURE_CLIENT_ID` triggers Entra error | L          | M      | Backend app reg's redirect URIs need `https://platform.marketsignal.ai/auth/callback` added (operator action, §10). If skipped, MSAL flows fail at first sign-in. Documented in operator pre-flight checklist  |

## 10. Operator Pre-Flight Checklist (gate before merge)

Operator confirms each item BEFORE merge:

- [ ] **DNS:** `platform.marketsignal.ai` A record → `vmPublicIp` from Bicep output (TTL ≤ 300s); verified with `dig +short platform.marketsignal.ai`
- [ ] **GitHub Secrets:** `VM_SSH_PRIVATE_KEY` added (ed25519 private key matching the public key in Bicep)
- [ ] **GitHub repo Variables:** `MSAI_HOSTNAME=platform.marketsignal.ai`, `MSAI_REGISTRY` (= `ACR_LOGIN_SERVER` value), `MSAI_BACKEND_IMAGE=msai-backend`, `MSAI_FRONTEND_IMAGE=msai-frontend`, `VM_PUBLIC_IP`, `VM_SSH_USER=msaiadmin`, `RESOURCE_GROUP=msaiv2_rg`, `KV_NAME` (from Slice 1 outputs), `VM_SSH_KNOWN_HOSTS` (multi-line value: VM's host key, captured during Slice 1 acceptance via `ssh-keyscan -t ed25519 <vmPublicIp>`). `NEXT_PUBLIC_API_URL` updated to `https://platform.marketsignal.ai/api/v1`.
- [ ] **KV seeded:** all 8 REQUIRED + 5 OPTIONAL secrets present per `scripts/msai-render-env.service`. `CORS_ORIGINS` value is `["https://platform.marketsignal.ai"]`. (Phase 1: the Operator KV Secrets Officer role granted in Slice 1 enables seeding.)
- [ ] **Backend app reg redirect URIs:** added `https://platform.marketsignal.ai/auth/callback` to backend's Entra app reg (operator action; takes effect tenant-side immediately)
- [ ] **Hawk's gate done:** `scripts/backup-to-blob.sh` ran successfully against empty prod Postgres; blob list output captured in PR description
- [ ] **Contrarian's gate done:** rehearsal RG smoked clean, RG torn down, evidence in PR description
- [ ] **Frontend image rebuilt:** after `NEXT_PUBLIC_API_URL` Variable update, `gh workflow run build-and-push.yml` triggered; new frontend `<sha7>` exists in ACR

Items NOT covered by this slice (deferred follow-ups, will surface again in Slice 4 or beyond):

- Dedicated frontend Entra SPA app registration
- Active `live_deployments` hard gate
- Nightly backup cron
- Log Analytics dashboards + alert rules

## 11. Research Summary

Research brief (Phase 2) is at `docs/research/2026-05-10-deploy-pipeline-ssh-deploy-and-first-deploy.md`. Key findings that diverge from this PRD's initial assumptions:

- **VM uses its own system-assigned MI for `az acr login`** instead of the CI-token-through-stdin pattern (research §6). Strictly simpler + no token-in-stdin attack surface. PRD §3 above was updated to reflect.
- **`webfactory/ssh-agent@v0.9.1`** (NOT `appleboy/ssh-action`) — supports both `scp` + `ssh` in same job (research §1).
- **`workflow_run` does NOT auto-filter on success** — explicit `if: github.event.workflow_run.conclusion == 'success'` required (research §5).
- **Caddyfile uses `handle /api/*`** (NOT `handle_path`) — backend serves `/api/v1/...` and prefix-stripping would 404 (research §3).
- **Caddyfile validation runs BEFORE `docker compose pull`** so a typo doesn't waste pull bandwidth (research §3 + Open Risks #6).
- **`--auth-mode login` slow on large `upload-batch`** — Slice 3's Hawk's-gate dump is fine; Slice 4 nightly Parquet mirror MUST migrate to `azcopy` (research §4 finding 5).

## 12. Acceptance Criteria Summary

Slice 3 ships when:

1. `deploy.yml` runs green on a no-op push to main, deploying the matching `<sha7>` image pair to prod
2. `https://platform.marketsignal.ai/health` returns 200 with a valid LE cert
3. Rollback is exercised at least once (the rehearsal RG smoke is a sufficient demonstration)
4. Hawk's gate has been honored with evidence in the PR
5. Contrarian's gate has been honored with evidence in the PR
6. First real prod deploy passes 5/5 acceptance smoke
