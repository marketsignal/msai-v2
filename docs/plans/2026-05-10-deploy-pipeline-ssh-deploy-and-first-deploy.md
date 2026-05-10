# Deployment Pipeline Slice 3: SSH Deploy + First Real Production Deploy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use `- [ ]` checkbox syntax.

**Goal:** Land `.github/workflows/deploy.yml` (OIDC + `webfactory/ssh-agent` SSH-from-runner pattern) and `scripts/deploy-on-vm.sh` (per-VM idempotent deploy script with rollback). Add Caddy 2 reverse-proxy + automatic Let's Encrypt to `docker-compose.prod.yml` fronted by `platform.marketsignal.ai`. Update `scripts/backup-to-blob.sh` to use Bicep outputs + system-assigned MI. Enable `msai-render-env.service` on the VM at first deploy. Run the first real production deploy after merge.

**Architecture:** Council-ratified Approach A, Slice 3 of 4 (per [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md)). Two key research-driven deviations from PRD: (1) ACR auth on the VM uses the VM's system-assigned MI directly (`az login --identity && az acr login --name`) instead of the originally-PRD'd "mint token in CI, pipe over SSH stdin" pattern — strictly simpler + strictly more secure (no token in stdin attack surface). (2) SSH primitive is `webfactory/ssh-agent@v0.9.1` (loads key into ssh-agent, supports both `scp` + `ssh` in same job) over `appleboy/ssh-action` (single-step but separate `scp-action` needed). Caddy uses `handle /api/*` (NOT `handle_path` — backend serves `/api/v1/...` and prefix-stripping would 404). Caddyfile is validated before `docker compose pull` so a typo doesn't waste pull bandwidth.

**Tech Stack:** GitHub Actions (ubuntu-24.04), `webfactory/ssh-agent@v0.9.1`, `azure/login@v2`, Docker Compose plugin v2.40+, Caddy 2.10.x (`caddy:2-alpine`), Azure CLI 2.71+, Azure Container Registry, Azure Key Vault + system-assigned MI, Bicep CLI 0.43.8.

---

## Approach Comparison

**PRE-DONE — council-ratified.** The Slice 3 scope was selected during the 4-slice decomposition at [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) §Slice 3. The same verdict folded the Contrarian's "vertical slice" objection (Approach B) into Blocking Objection #4: Slice 3 cannot merge until full VM deploy path is rehearsed end-to-end in a throwaway RG.

### Chosen Default

**Slice 3 of Approach A.** Concretely:

- `.github/workflows/deploy.yml` (workflow_run on Slice 2 + workflow_dispatch with `git_sha` input → OIDC → SSH to VM → `scripts/deploy-on-vm.sh` → success-signal probes from runner)
- `scripts/deploy-on-vm.sh` (runs ON the VM as `sudo`; idempotent; classified failure markers; rollback on probe failure)
- `Caddyfile` + `caddy` service in `docker-compose.prod.yml` (auto-LE on `platform.marketsignal.ai`)
- `scripts/backup-to-blob.sh` rewrite (uses Bicep outputs + MI; replaces hardcoded `msaistorage` from older Phase 1 sketch)
- `docs/runbooks/slice-3-rehearsal.md` (Contrarian's-gate procedure)
- Slice 1 systemd unit (`msai-render-env.service`) gets enabled at first deploy via `deploy-on-vm.sh` (no Bicep change)

### Best Credible Alternative

**Approach B (vertical slice — bundle Slices 1+2+3+4 into one PR).** The Contrarian's preferred shape, partially folded into A as Blocking Objection #4. Approach C (MVP-first, defer KV) was rejected 5/5.

### Scoring (fixed axes — recap from slicing verdict)

| Axis                  | Default A (Slice 3)                  | Alternative B      |
| --------------------- | ------------------------------------ | ------------------ |
| Complexity            | M                                    | H                  |
| Blast Radius          | M (first prod deploy)                | H (one massive PR) |
| Reversibility         | M (image rollback by SHA)            | L (one big bisect) |
| Time to Validate      | M (~hours w/ rehearsal + acceptance) | H (~days)          |
| User/Correctness Risk | M                                    | H                  |

### Cheapest Falsifying Test

**Contrarian's gate (mandatory).** Provision a throwaway RG `msaiv2-rehearsal-<date>`, run Slice 1 IaC against it, build images via Slice 2 once, dispatch deploy.yml against the rehearsal RG (with `RESOURCE_GROUP` workflow input override), confirm 5/5 acceptance probes, tear RG down. ~2 hours wall-clock; falsifies any integration assumption that didn't survive Slices 1+2's static review.

## Contrarian Verdict

**VALIDATE (with mandatory rehearsal gate).** The Contrarian's OBJECT on Approach A's horizontal slicing (filed during the slicing council) was preserved as **Blocking Objection #4**: Slice 3 cannot merge until the VM deploy path is rehearsed end-to-end. We honor this gate in the operator pre-flight checklist (PRD §10) and in the Phase 5.4 E2E plan below — the rehearsal-RG smoke is the cheapest falsifying test for a multi-component path.

## Plan-Review Iter 1: Council on NSG SSH Gap (P0)

A P0 surfaced in plan-review iter 1: Slice 1's NSG only allows SSH inbound from `operatorIp/32`, blocking GH-hosted runner SSH. Per critical-rules CONTRARIAN-GATE, escalated to `/council`. **5/5 advisors reject Alternative** (static GitHub IP ranges). 2 APPROVE Default, 3 CONDITIONAL on hardening. **Contrarian found 2 issues the others missed** (Bicep `securityRules:` inline-property drift bomb; concurrent-deploy `priority 1000` collision). Council verdict: adopt Default with 5 mandatory fixes (Bicep child-resource refactor, separate cleanup job, janitor reaper workflow, ADR + runbook, rule names with `${run_id}-${run_attempt}`). See new tasks T13–T16 + T05a/T06d/T06e amendments below.

---

## Files

### Created

- **`.github/workflows/deploy.yml`** — single workflow file: triggers on `workflow_run` (Slice 2 success on main) + `workflow_dispatch` (with `git_sha` and `resource_group` inputs); job-level `if:` gates on `event_name == workflow_dispatch || workflow_run.conclusion == 'success'`; uses `webfactory/ssh-agent@v0.9.1` for the SSH key, `azure/login@v2` for OIDC, scp + ssh to VM, runs runner-side `/health` + TLS probes after VM script returns 0. ~150 lines.

- **`scripts/deploy-on-vm.sh`** — runs ON the VM as `sudo bash`; positional args: `<git_sha7> [resource_group]`. Idempotent. Steps: (1) ENV validate; (2) `az login --identity && az acr login --name`; (3) record current `/run/msai-images.env` to `.last-good`; (4) write new `/run/msai-images.env` with `MSAI_GIT_SHA=<sha>`; (5) restart `msai-render-env.service` (refresh KV secrets) — idempotent enable on first call; (6) Caddyfile validation (`docker compose run --rm caddy validate --config /etc/caddy/Caddyfile`); (7) `docker compose pull` (explicit service list, default profile only); (8) `docker compose up -d --wait --wait-timeout 300 <explicit list>`; (9) probes: backend `/health` 200, `/ready` 200, soft-`XINFO GROUPS msai:live:commands` (only logged), Caddy `https://${MSAI_HOSTNAME}/health` 200; (10) on any FAIL*PROBE*\*, restore `.last-good`, re-run `up -d --wait`, exit `FAIL_ROLLBACK_OK` or `FAIL_ROLLBACK_BROKEN`. ~250 lines incl. comments.

- **`Caddyfile`** — at repo root; `{$MSAI_HOSTNAME}` site address; `handle /api/*` → `reverse_proxy backend:8000` (PREFIX PRESERVED — comment explains why); `handle` (catch-all) → `reverse_proxy frontend:3000`; `encode gzip zstd`; `log` to stdout. ~30 lines.

- **`docs/runbooks/slice-3-rehearsal.md`** — Contrarian's-gate procedure: provision throwaway RG, deploy Slice 1 against it, manually build Slice 2 images (or reuse latest main images), `gh workflow run deploy.yml -f resource_group=<throwaway> -f git_sha=<sha>`, run 5/5 acceptance probes, tear down. Includes `crt.sh` LE rate-limit pre-check for `marketsignal.ai`. ~120 lines.

- **`docs/runbooks/slice-3-first-deploy.md`** — first real prod deploy procedure: confirm operator pre-flight checklist (PRD §10) is complete, `gh workflow run deploy.yml -f git_sha=<merge-sha>` (manual to avoid `workflow_run` silently-skipped first-run edge case), 5/5 acceptance smoke, log evidence to CHANGELOG. ~80 lines.

- **`tests/infra/test_workflow_deploy.sh`** — actionlint on `deploy.yml`, plus shellcheck on `deploy-on-vm.sh` and updated `backup-to-blob.sh`, plus assertions: `webfactory/ssh-agent` step present, `azure/login@v2` present, `concurrency.cancel-in-progress: false`, `if:` gate on workflow_run.conclusion, `git_sha` regex validation step. Mirrors existing `tests/infra/test_bicep.sh` style.

- **`tests/infra/test_caddyfile.sh`** — runs `docker run --rm -v $(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro -e MSAI_HOSTNAME=test.example.com caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile` and asserts exit 0; runs against a fixture and asserts a syntax error fails non-zero. ~25 lines.

- **`tests/infra/test_deploy_on_vm.bats`** (or `.sh` if bats not desired) — bats-style unit tests for `deploy-on-vm.sh`'s pure helpers (failure-marker classification, env validation, image-SHA file rendering). Mocks `az`, `docker`, `curl`, `redis-cli` via PATH override. ~120 lines.

### Modified

- **`docker-compose.prod.yml`** — three edits:
  1. **Add `caddy` service** after `frontend`: image `caddy:2-alpine`, ports `80:80` and `443:443` published to host, named volumes `caddy_data:/data` + `caddy_config:/config`, `Caddyfile` bind-mounted ro at `/etc/caddy/Caddyfile`, env var `MSAI_HOSTNAME: ${MSAI_HOSTNAME:?Set MSAI_HOSTNAME}`, `depends_on: backend: service_healthy, frontend: service_started`, `restart: unless-stopped`.
  2. **Remove host port 8000 publish from backend** (or convert to `127.0.0.1:8000:8000` for VM-local debugging only) — Caddy is now the only public ingress on 80/443; backend should not be reachable directly from the public internet at 8000. Belt-and-braces with the NSG (Slice 1 NSG only allows 22/80/443 inbound; this prevents accidental future NSG changes from exposing 8000).
  3. **Same for frontend port 3000:** `127.0.0.1:3000:3000` so Caddy reaches it via the compose network at `frontend:3000` and direct host access is loopback-only.
  4. **Add `caddy_data` and `caddy_config` to `volumes:` block** with explicit `name:` to match the `msai_postgres_data` / `msai_app_data` pattern.

- **`scripts/backup-to-blob.sh`** — full rewrite per research §4:
  - Read `STORAGE_ACCT` and `CONTAINER` from `az deployment group show ... --query 'properties.outputs.{backupsStorageAccount,backupsContainerName}.value'`.
  - `az login --identity` for MI authentication.
  - `pg_dump | az storage blob upload --auth-mode login --account-name $STORAGE_ACCT --container-name $CONTAINER --name backup-<UTC-iso>.sql.gz --file /dev/stdin` — single-blob streaming upload (no temp file on disk).
  - Parquet mirror: keep the existing `cp -r` + `az storage blob upload-batch --auth-mode login` pattern for now; flag in script comment that Slice 4 should switch to `azcopy --recursive` (research §4 finding 5).
  - Fail with explicit remediation hints on missing Bicep outputs / unassigned MI / `AuthorizationFailure`.

- **`docs/research/2026-05-10-deploy-pipeline-ssh-deploy-and-first-deploy.md`** — no edits; brief is final.

- **`docs/prds/deploy-pipeline-ssh-deploy-and-first-deploy.md`** — Phase 3.3 plan-review will likely surface two PRD revisions; apply during plan-review:
  - §3 (Pablo persona) — drop "fetches a one-shot ACR access token (handed to the VM via SSH stdin)" → "VM uses its own MI to `az acr login`; no token transit through CI" (research §6).
  - §11 (Phase 2 research targets) — strike completed; or convert to a "Research summary" pointer to the brief.

- **`docs/CHANGELOG.md`** — add Slice 3 entry per existing format (added at Phase 6.2).

- **`.claude/local/state.md`** — workflow checklist updates per Phase boundaries (already in flight from Phase 0).

### Build context for runtime image consumers

Slice 3 doesn't add new images (Slice 2 produced `msai-backend:<sha>` + `msai-frontend:<sha>`). It consumes them. The only new container is Caddy (`caddy:2-alpine` from Docker Hub — no build).

---

## E2E Use Cases (Phase 3.2b)

Project type per CLAUDE.md `## E2E Configuration`: **fullstack** (API + UI). API-first ordering applies. Slice 3 is mostly CI/infra so most use cases verify the deploy pipeline itself, not application behavior. Use cases below stage in this plan; the ones that pass during Phase 5.4 graduate to `tests/e2e/use-cases/deploy-pipeline-ssh.md` in Phase 6.2b.

#### UC-1: Deploy a known-good SHA via workflow_dispatch (rehearsal RG)

- **Interface:** CLI (`gh workflow run`) + API (probes against deployed backend) + UI (probe Caddy-fronted frontend root)
- **Setup:** Operator has already provisioned the rehearsal RG and seeded its KV (per `docs/runbooks/slice-3-rehearsal.md`). Slice 2 image pair `<sha>` exists in ACR. `VM_SSH_PRIVATE_KEY` and known_hosts populated for the rehearsal VM.
- **Steps:**
  1. `gh workflow run deploy.yml -f git_sha=<sha7> -f resource_group=msaiv2-rehearsal-<date>`
  2. `gh run watch <run-id>` until completion
  3. Probe: `curl -sf https://platform-rehearsal.marketsignal.ai/health` → 200
  4. Probe: `curl -sf https://platform-rehearsal.marketsignal.ai/ready` → 200
  5. Probe: `curl -sI https://platform-rehearsal.marketsignal.ai/` → 200, `content-type: text/html`
  6. Probe: `openssl s_client -connect platform-rehearsal.marketsignal.ai:443 -servername platform-rehearsal.marketsignal.ai </dev/null 2>/dev/null | openssl x509 -noout -issuer` shows `Let's Encrypt`
  7. Probe: `curl -sf https://platform-rehearsal.marketsignal.ai/api/v1/auth/me` returns 401 (proxied to backend, prefix preserved → not 404)
- **Verification:** All 6 probes return expected outcomes. `gh run view --log` shows no FAIL*PROBE*\* in the deploy-on-vm.sh log.
- **Persistence:** Re-run the same probes after 60s — same results (no flapping).

#### UC-2: Rollback to a previous SHA (rehearsal RG)

- **Interface:** CLI + API
- **Setup:** UC-1 completed with `<sha-A>`. A second image `<sha-B>` exists in ACR.
- **Steps:**
  1. `gh workflow run deploy.yml -f git_sha=<sha-B>` against rehearsal RG (auto-via workflow_dispatch input)
  2. Confirm UC-1 probes pass against `<sha-B>`
  3. `gh workflow run deploy.yml -f git_sha=<sha-A>` (rollback)
  4. Confirm UC-1 probes still pass; backend health response now reports `<sha-A>` (if backend `/health` includes a SHA — current behavior: it does not, so check via `docker compose ps` showing the rolled-back image tag instead)
- **Verification:** Both deploys succeed. `docker compose ps --format json | jq '.Image'` on the VM after rollback shows `msai-backend:<sha-A>`.
- **Persistence:** N/A — this IS the persistence test.

#### UC-3: Deliberate failure triggers automatic rollback

- **Interface:** CLI (deploy) + API (probes)
- **Setup:** Rehearsal RG has `<sha-A>` deployed and healthy. Stage a deliberate breakage: build `<sha-broken>` with a syntactically-valid but semantically-bad change (e.g., backend startup intentionally fails — easiest: introduce a missing required env var that the backend asserts on at startup, OR push a backend image that exits 1 immediately).
- **Steps:**
  1. `gh workflow run deploy.yml -f git_sha=<sha-broken>` against rehearsal RG
  2. `gh run watch` — observe failure
- **Verification:** Workflow exits non-zero. `gh run view --log` shows `FAIL_PROBE_HEALTH` followed by `FAIL_ROLLBACK_OK`. `docker compose ps` on the VM after failure shows `<sha-A>` running (rolled back). UC-1 probes pass against `<sha-A>`.
- **Persistence:** Re-run UC-1 probes after 60s — `<sha-A>` still running, all probes pass.

#### UC-4: Backup-to-Blob (Hawk's gate)

- **Interface:** CLI (`scripts/backup-to-blob.sh`) + Azure CLI (`az storage blob list`)
- **Setup:** Rehearsal RG has Postgres up but empty (no app data yet). VM has system-assigned MI with Storage Blob Data Contributor.
- **Steps:**
  1. SSH to rehearsal VM
  2. `sudo /opt/msai/scripts/backup-to-blob.sh` (operator has placed scripts in `/opt/msai/` per Slice 1 cloud-init or via scp during this rehearsal)
  3. From operator's machine: `az storage blob list --auth-mode login --account-name <bicep-output-storage-acct> --container-name msai-backups --prefix backup- --query "[?starts_with(name, 'backup-$(date -u +%Y%m%d)')].name" -o tsv`
- **Verification:** Step 3 returns ≥1 blob whose name starts with today's UTC date.
- **Persistence:** Re-run step 3 — same blob still present.

#### UC-5: First real prod deploy (post-merge, operator gate)

- **Interface:** CLI + API + UI (Caddy)
- **Setup:** PR merged, all pre-flight checklist items in PRD §10 complete.
- **Steps:** UC-1 procedure run against `msaiv2_rg` (default, no `resource_group` override). 5/5 probes pass.
- **Verification:** Same as UC-1 against the production hostname `platform.marketsignal.ai`. Evidence captured in CHANGELOG.
- **Persistence:** Re-probe 5 minutes after deploy completes — same.

#### UC-6: Caddyfile validation catches typos before pull

- **Interface:** CLI (`deploy-on-vm.sh` invocation)
- **Setup:** Stage a deliberate Caddyfile typo on the VM (e.g., `handle /api/*` → `handel /api/*`).
- **Steps:** Run `sudo /tmp/deploy-on-vm.sh <some-sha>` directly on the VM (not via deploy.yml).
- **Verification:** Script exits with `FAIL_CADDY_VALIDATE` BEFORE running `docker compose pull`. Existing Caddy container keeps running. `docker compose ps` unchanged.
- **Persistence:** N/A.

---

## Tasks

> **Concurrency model:** Most tasks are file-level disjoint and parallelizable. Caddyfile + Caddy compose-service edits are coupled (T03 + T04) — serialize. `deploy.yml` and `deploy-on-vm.sh` reference each other's interface (positional args) — author the contract first (T05a), then split.

### T01 — Caddyfile

- [ ] Create `Caddyfile` at repo root with `handle /api/*` (NOT `handle_path` — comment explains), `handle` catch-all to `frontend:3000`, `encode gzip zstd`, `log` directive.
- [ ] Add explanatory comment block at top: env vars expected (`MSAI_HOSTNAME`), why prefix is preserved.

### T02 — Caddyfile validation test

- [ ] Create `tests/infra/test_caddyfile.sh`. Asserts `caddy validate` passes with `MSAI_HOSTNAME=test.example.com` and fails with intentional syntax error (use a temp fixture).

### T03 — Caddy service in docker-compose.prod.yml

- [ ] Add `caddy` service after `frontend`: image `caddy:2-alpine`, ports `80:80` + `443:443`, named volumes `caddy_data:/data` + `caddy_config:/config`, bind-mount `./Caddyfile:/etc/caddy/Caddyfile:ro`, env `MSAI_HOSTNAME: ${MSAI_HOSTNAME:?Set MSAI_HOSTNAME}`, `depends_on: backend: service_healthy, frontend: service_started`, `restart: unless-stopped`, `deploy.resources.limits` (cpus 0.5, memory 512M).
- [ ] Add `caddy_data` and `caddy_config` to top-level `volumes:` block with `name:` per existing pattern.

### T04 — Lock backend/frontend host ports to localhost-only

- [ ] In `docker-compose.prod.yml`: change `backend.ports` from `"8000:8000"` to `"127.0.0.1:8000:8000"` (VM-local debugging only).
- [ ] Same for `frontend.ports`: `"127.0.0.1:3000:3000"`.
- [ ] Update header comment block to document that Caddy is the only public ingress (80/443).

### T05a — deploy.yml ↔ deploy-on-vm.sh interface contract

- [ ] Define and document positional args + env contract for `deploy-on-vm.sh`:
  - `$1 = git_sha7` (regex-validated by deploy.yml before SSH)
  - **Env passthrough mechanism (P1 plan-review iter 1):** because `sudo` strips env by default and editing sudoers for env keep is brittle, deploy.yml writes a temp env file on the VM via a heredoc-over-ssh BEFORE invoking sudo, then `deploy-on-vm.sh` sources it from a fixed path (`/tmp/deploy-env-${run_id}.env`). Pattern: `ssh "$VM_USER@$VM_IP" "cat > /tmp/deploy-env-${RUN_ID}.env" <<EOF\nMSAI_ACR_NAME=...\nMSAI_HOSTNAME=...\n...\nEOF` then `ssh ... "sudo bash /tmp/deploy-on-vm-${RUN_ID}.sh ${SHA} /tmp/deploy-env-${RUN_ID}.env"`. Script's `$2` is the env-file path; script sources it as its first action. Cleanup on every exit path (success or fail).
  - Required env (sourced by deploy-on-vm.sh from `$2`): `MSAI_ACR_NAME`, `MSAI_ACR_LOGIN_SERVER`, `MSAI_REGISTRY` (= login_server), `MSAI_BACKEND_IMAGE`, `MSAI_FRONTEND_IMAGE`, `MSAI_HOSTNAME`, `KV_NAME`, `RESOURCE_GROUP`, `DEPLOYMENT_NAME`
  - Output: stdout = human log, stderr = errors, exit code 0 = success, non-zero with one of {`FAIL_ENV`, `FAIL_AZ_LOGIN`, `FAIL_ACR_LOGIN`, `FAIL_RENDER_ENV`, `FAIL_CADDY_VALIDATE`, `FAIL_PULL`, `FAIL_MIGRATE`, `FAIL_PROBE_HEALTH`, `FAIL_PROBE_READY`, `FAIL_PROBE_TLS`, `FAIL_ROLLBACK_OK`, `FAIL_ROLLBACK_BROKEN`} on the LAST line of stderr.
- [ ] Document in `scripts/deploy-on-vm.sh` header (since the file doesn't exist yet, this task creates the file with header-only and a `main "$@"` stub; T05b/c fill in the body).

### T05b — deploy-on-vm.sh: pre-pull steps

- [ ] Strict bash header (`set -euo pipefail`). Trap to print `FAIL_<phase>` on error.
- [ ] **P1 plan-review iter 1:** Source the env-file passed as `$2` as the FIRST action (before any other validation). `[[ -r "$2" ]] || { echo "FAIL_ENV: env file $2 not readable"; exit 1; }`. Then `set -a; . "$2"; set +a`.
- [ ] ENV validation (`: "${VAR:?...}"` for each required env including **MSAI_HOSTNAME** — required by Caddy compose-service `:?` guard and by Caddyfile interpolation) → exit `FAIL_ENV` on missing.
- [ ] `az login --identity --output none` → exit `FAIL_AZ_LOGIN` on failure.
- [ ] `az acr login --name "$MSAI_ACR_NAME"` → exit `FAIL_ACR_LOGIN`.
- [ ] Record current `/run/msai-images.env` to `/run/msai-images.last-good.env` (cp -p; tolerate file-not-exists for first deploy → write a sentinel `MSAI_FIRST_DEPLOY=1` so the rollback path knows to no-op gracefully).
- [ ] **P1 plan-review iter 1:** Write new `/run/msai-images.env` with `MSAI_GIT_SHA=$1`, `MSAI_REGISTRY`, `MSAI_BACKEND_IMAGE`, `MSAI_FRONTEND_IMAGE`, **`MSAI_HOSTNAME`** (added — Caddy needs it via `--env-file`) (atomic rename via `.tmp`). NOT included: secrets (those live in `/run/msai.env` from the render service).
- [ ] **P1 plan-review iter 1:** The Slice 1 systemd unit has `Environment="KV_NAME=__SLICE3_FILLS_THIS__"` placeholder. Slice 3 deploy-on-vm.sh writes a drop-in override at `/etc/systemd/system/msai-render-env.service.d/override.conf` with `[Service]\nEnvironment="KV_NAME=$KV_NAME"\n` (idempotent — overwrite each deploy in case KV name ever changes), then `systemctl daemon-reload`. Drop-in pattern is preferred over `sed -i` editing the planted unit because it survives Slice 1 cloud-init re-runs and is the standard systemd override mechanism.
- [ ] `systemctl enable --now msai-render-env.service` (idempotent), then `systemctl restart msai-render-env.service` to refresh `/run/msai.env` from KV → exit `FAIL_RENDER_ENV` on non-zero. Verify post-restart that `/run/msai.env` exists and is non-empty (sanity check).
- [ ] **P2 plan-review iter 1:** Pre-pull caddy image to avoid auto-pull latency in the validate step: `docker compose ${COMPOSE_FLAGS} pull caddy`. Acceptable cost (~5-30s on cold cache; no-op once cached).
- [ ] Run `docker compose ${COMPOSE_FLAGS} run --rm caddy validate --config /etc/caddy/Caddyfile` → exit `FAIL_CADDY_VALIDATE` on failure. Where `COMPOSE_FLAGS="--project-name msai -f /opt/msai/docker-compose.prod.yml --env-file /run/msai.env --env-file /run/msai-images.env"` (defined once near top of script).

### T05c — deploy-on-vm.sh: pull + up + probes + rollback

- [ ] Define DEFAULT_PROFILE_SERVICES var = explicit list (no broker services).
- [ ] `docker compose ... pull $DEFAULT_PROFILE_SERVICES` → exit `FAIL_PULL`.
- [ ] `docker compose ... up -d --wait --wait-timeout 300 $DEFAULT_PROFILE_SERVICES` (migrate first via depends_on, then long-running) → exit `FAIL_MIGRATE` if migrate failed (parse compose output).
- [ ] Probe: backend `/health` 200 (curl from VM, hit `http://127.0.0.1:8000/health` via the localhost-published port — bypasses Caddy for fast-fail on backend itself); 30 retries × 2s. Exit `FAIL_PROBE_HEALTH` if all fail.
- [ ] Probe: backend `/ready` 200 same pattern. Exit `FAIL_PROBE_READY`.
- [ ] **P1 plan-review iter 1:** Soft probe via `docker compose ${COMPOSE_FLAGS} exec -T redis redis-cli EXISTS msai:live:commands` (NOT `redis-cli -h 127.0.0.1` — Redis is not host-published in prod compose, only on the compose network). If output is 0, log "WARN: live-supervisor stream not yet created — OK for default-profile deploy" and continue. Never hard-fail here per PRD §6 D-1.
- [ ] Probe: `curl -sf https://${MSAI_HOSTNAME}/health` 200 (full TLS-through-Caddy round-trip). 60 retries × 5s (LE issuance can take ~30s on first deploy). Exit `FAIL_PROBE_TLS`.
- [ ] On any FAIL*PROBE*\*: trap kicks in → restore `/run/msai-images.last-good.env` → re-run pull + up — if successful exit `FAIL_ROLLBACK_OK` (deploy failed but rolled back); if rollback also failed exit `FAIL_ROLLBACK_BROKEN` (manual intervention required).
- [ ] On full success: emit `SUCCESS sha=$1` to stdout and exit 0.

### T05d — deploy-on-vm.sh unit tests

- [x] **Phase 4 implementation: SCOPED OUT.** `bash -n` syntax + `shellcheck` lint + `tests/infra/test_workflow_deploy.sh` grep assertions cover the script contract (failure-marker presence, ACR-login-via-MI pattern, `--project-name msai`, caddy validate command shape). Full PATH-mocking of `az`/`docker`/`systemctl`/`redis-cli` adds ~120 lines of bats fixtures with limited incremental confidence over the rehearsal-RG smoke (Contrarian's gate). The integration test IS the falsifier here. Slice 4 ops focus can revisit if orphan-rule patterns suggest unit-level coverage gaps.

### T06a — deploy.yml: structure + triggers + concurrency

- [ ] `on:` block: `workflow_run` (workflows: ["Build and Push Images"], types: [completed], branches: [main]) + `workflow_dispatch` (inputs: `git_sha` optional string, `resource_group` optional string default `msaiv2_rg`).
- [ ] `permissions: id-token: write, contents: read`.
- [ ] `concurrency: group: deploy-msai, cancel-in-progress: false`.
- [ ] Job-level `if: ${{ github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success' }}`.

### T06b — deploy.yml: SHA resolution + validation

- [ ] Step `Resolve git_sha`: if `inputs.git_sha` set, use it (validate `^[0-9a-f]{7}$`); else use `${GITHUB_SHA::7}` (workflow's commit). Emit `short_sha` output.
- [ ] Step `Validate git_sha format`: regex check, exit 1 with clear error on mismatch.

### T06c — deploy.yml: Azure login + SSH agent + known_hosts

- [ ] `actions/checkout@v4.2.2`.
- [ ] `azure/login@v2` — same Variables pattern as Slice 2.
- [ ] `webfactory/ssh-agent@v0.9.1` with `ssh-private-key: ${{ secrets.VM_SSH_PRIVATE_KEY }}`.
- [ ] Inline step `Trust VM host key`: `mkdir -p ~/.ssh && echo "${{ vars.VM_SSH_KNOWN_HOSTS }}" >> ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts`.

### T06d — deploy.yml: scp + ssh + cleanup

- [ ] `scp scripts/deploy-on-vm.sh "${{ vars.VM_SSH_USER }}@${{ vars.VM_PUBLIC_IP }}:/tmp/deploy-on-vm-${{ github.run_id }}.sh"`.
- [ ] **P1 plan-review iter 1:** Build env file on the VM via heredoc-over-ssh BEFORE invoking sudo (avoids sudo env-strip + sudoers env_keep brittleness):
  ```yaml
  - name: Stage deploy env on VM
    run: |
      ssh "${VM_USER}@${VM_IP}" "umask 077 && cat > /tmp/deploy-env-${RUN_ID}.env" <<EOF
      MSAI_ACR_NAME=${{ vars.ACR_NAME }}
      MSAI_ACR_LOGIN_SERVER=${{ vars.ACR_LOGIN_SERVER }}
      MSAI_REGISTRY=${{ vars.ACR_LOGIN_SERVER }}
      MSAI_BACKEND_IMAGE=${{ vars.MSAI_BACKEND_IMAGE }}
      MSAI_FRONTEND_IMAGE=${{ vars.MSAI_FRONTEND_IMAGE }}
      MSAI_HOSTNAME=${{ vars.MSAI_HOSTNAME }}
      KV_NAME=${{ vars.KV_NAME }}
      RESOURCE_GROUP=${{ inputs.resource_group || vars.RESOURCE_GROUP }}
      DEPLOYMENT_NAME=${{ vars.DEPLOYMENT_NAME }}
      EOF
  ```
- [ ] Then SSH-exec: `ssh "${VM_USER}@${VM_IP}" "sudo bash /tmp/deploy-on-vm-${RUN_ID}.sh ${SHA} /tmp/deploy-env-${RUN_ID}.env"`.
- [ ] **Cleanup (always)** via `if: always()` job step: `ssh ... rm -f /tmp/deploy-on-vm-${{ github.run_id }}.sh /tmp/deploy-env-${{ github.run_id }}.env`. Also runs on workflow-cancel.

### T06e — deploy.yml: runner-side acceptance probes

- [ ] After SSH step succeeds, run from runner: `curl -sf https://${{ vars.MSAI_HOSTNAME }}/health` (5 retries × 5s). Confirms Caddy + LE end-to-end from the public internet, not just from the VM.
- [ ] `curl -sI https://${{ vars.MSAI_HOSTNAME }}/` returns 200 + html.
- [ ] `openssl s_client -connect ${{ vars.MSAI_HOSTNAME }}:443 -servername ${{ vars.MSAI_HOSTNAME }} </dev/null | openssl x509 -noout -issuer | grep -q "Let's Encrypt"`.
- [ ] If any of the 3 probes fails: log + exit 1 (the on-VM script already rolled back the deploy, but the runner-side probe failure surfaces public-reachability issues — DNS, NSG, LE — that VM-local probes miss).

### T07 — deploy.yml workflow lint test

- [ ] `tests/infra/test_workflow_deploy.sh` — actionlint on deploy.yml + grep-assertions per the bullets in §Files.

### T08 — backup-to-blob.sh rewrite

- [ ] Rewrite per research §4. Read Bicep outputs for storage account + container. `az login --identity`. `pg_dump | az storage blob upload --auth-mode login --file /dev/stdin --name backup-<UTC>.sql.gz`.
- [ ] Add comment header pointing to research §4 finding 5 + slice-4 azcopy carry-over.
- [ ] shellcheck-clean.

### T09 — Slice-3 rehearsal runbook

- [ ] `docs/runbooks/slice-3-rehearsal.md` per the §Files spec.
- [ ] Includes `crt.sh` LE rate-limit pre-check.
- [ ] Includes the exact `az group create` / `az group delete` commands.

### T10 — Slice-3 first-deploy runbook

- [ ] `docs/runbooks/slice-3-first-deploy.md` per the §Files spec.
- [ ] References operator pre-flight checklist (PRD §10).
- [ ] References the workflow_run silently-skipped first-run edge case (research §5 finding 6) — operator triggers via `workflow_dispatch` for first run after merge.

### T11 — PRD revisions from research findings

- [ ] Apply two PRD edits per `### Modified` §Files entry (ACR auth simplification + research targets converted to research-summary).

### T12 — CHANGELOG entry (Phase 6.2 final)

- [ ] Add Slice 3 entry per existing format. Includes evidence pointers (PR #, rehearsal RG name + delete confirmation, first-deploy SHA + run URL).

---

## Council-Mandated Tasks (Plan-Review Iter 1, P0 NSG SSH gap)

### T13 — Refactor Bicep NSG `securityRules` from inline to child resources

- [ ] In `infra/main.bicep`: change the existing inline `securityRules:` block on `nsg` (lines 116-167 currently) to a parent NSG with NO `securityRules:` property, plus three separate `Microsoft.Network/networkSecurityGroups/securityRules@2024-01-01` child resources for `AllowSshFromOperator`, `AllowHttpInbound`, `AllowHttpsInbound`. **Why:** Contrarian's P0 — inline rules are a complete property and any future `az deployment group create` (Slice 4 etc.) will reconcile away transient deploy rules mid-SSH. Child resources are appended/merged on apply.
- [ ] Validate via `az deployment group what-if` that the refactor is a property-level rename only (no actual rule destruction).
- [ ] Apply against the rehearsal RG first; confirm SSH from `operatorIp` still works post-refactor.

### T14 — Custom-scoped RBAC for ghOidcMi NSG rule mutation

- [ ] In `infra/main.bicep`: add a `roleAssignment` granting the existing `ghOidcMi.properties.principalId` the built-in **Network Contributor** role scoped to `nsg.id` only (NOT subscription, NOT resource group). Built-in role ID `4d97b98b-1d4f-4787-a291-c67834d212e7`. Tighter custom-role-with-only-`securityRules/{read,write,delete}`-actions is a Phase 2 hardening (deferred per council Maintainer's compromise — Slice 3 documents the trade-off in the new ADR; Slice 4 or later can swap to custom role).
- [ ] Add the corresponding `Microsoft.Network/networkSecurityGroups/securityRules` write permission justification comment.

### T15 — Janitor reaper workflow

- [ ] Create `.github/workflows/reap-orphan-nsg-rules.yml`. Triggers: `schedule: cron: '*/15 * * * *'` + `workflow_dispatch`. Jobs:
  - `azure/login@v2` (same OIDC pattern).
  - `az network nsg rule list -g $RG --nsg-name $NSG_NAME --query "[?starts_with(name, 'gha-transient-')]"` — list candidates.
  - For each rule, parse `name` (format `gha-transient-${run_id}-${run_attempt}`), use `az monitor activity-log list` or rule's metadata to compute age; delete if >30min.
  - Emit Azure Monitor metric on each deletion (counts toward an alert rule wired in Slice 4).
- [ ] Concurrency: `concurrency: group: nsg-reaper, cancel-in-progress: true` (only one reaper at a time).
- [ ] Test (rehearsal RG): create 2 stale rules manually, run reaper, confirm both deleted; create 1 fresh rule, run reaper, confirm not deleted.

### T16 — ADR `docs/decisions/deploy-ssh-jit.md`

- [ ] New decision doc explaining: (a) why not self-hosted runner (lateral-movement; cite architecture verdict §3); (b) why not Bastion ($$, complexity); (c) why not static GH IP ranges (5/5 council reject; always-open SSH to all of GH egress = wider blast radius); (d) why not ACI-in-VNet jump (deferred — escape hatch if Slice 4 surfaces operational pain; cost ~$0.003/deploy, removes 4/5 failure modes per Contrarian; Slice 3 doesn't pay this cost yet).
- [ ] Document the 5 council-mandated mitigations with file pointers.
- [ ] Document the deferred-Phase-2 hardening: Azure Policy deny on `sourceAddressPrefix != <runner-cidr>` and custom RBAC role replacing Network Contributor.
- [ ] Document the leaked-rule recovery procedure in `docs/runbooks/deploy-pipeline.md` (or extend `slice-3-first-deploy.md`).

### T05a/T06d Amendments (already applied in iter-1 P1 fixes; reinforced by council)

- T06a: confirm `concurrency: group: deploy-msai, cancel-in-progress: false` (already in plan).
- T06d: change rule name from PRD's `GHActionsTransient-${RUN_ID}` to `gha-transient-${{ github.run_id }}-${{ github.run_attempt }}` for re-run uniqueness (Hawk + Maintainer).
- T06e (NEW): split cleanup into a separate job:

  ```yaml
  cleanup:
    needs: [deploy]
    if: always()
    runs-on: ubuntu-24.04
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: azure/login@v2
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
      - name: Delete transient SSH rule
        run: |
          az network nsg rule delete \
            --resource-group "${{ inputs.resource_group || vars.RESOURCE_GROUP }}" \
            --nsg-name "${{ vars.NSG_NAME }}" \
            --name "gha-transient-${{ github.run_id }}-${{ github.run_attempt }}" \
            --output none || true
  ```

  Separate job survives runner-VM kill (Hawk + Contrarian).

### T06b NEW — Open transient SSH rule (BEFORE all SSH steps)

- [ ] First step in `deploy` job after `azure/login@v2`:

  ```yaml
  - name: Resolve runner public IP
    id: runner-ip
    run: echo "ip=$(curl -sf https://api.ipify.org)" >> "$GITHUB_OUTPUT"

  - name: Open transient SSH rule
    run: |
      az network nsg rule create \
        --resource-group "${{ inputs.resource_group || vars.RESOURCE_GROUP }}" \
        --nsg-name "${{ vars.NSG_NAME }}" \
        --name "gha-transient-${{ github.run_id }}-${{ github.run_attempt }}" \
        --priority 200 \
        --direction Inbound \
        --access Allow \
        --protocol Tcp \
        --source-address-prefixes "${{ steps.runner-ip.outputs.ip }}/32" \
        --destination-port-ranges 22 \
        --description "Transient SSH from GH Actions runner. run_id=${{ github.run_id }} attempt=${{ github.run_attempt }}. Auto-deleted by cleanup job. Reaped by reap-orphan-nsg-rules.yml if leaked. See docs/decisions/deploy-ssh-jit.md."
  ```

  Priority 200 sits above the static `AllowSshFromOperator` (priority 100) and below the operator-IP rule's priority floor — operator SSH always works regardless of transient rule state.

### T13 spike (Contrarian's <30 min test)

- [ ] In rehearsal RG (or a temp `msaiv2-spike-<date>` RG): apply the refactored Bicep with NSG child-resource layout; manually create a `gha-transient-spike-1` rule via `az network nsg rule create`; re-run `az deployment group create -f infra/main.bicep` against the same RG. **Confirm:** the transient rule survives the redeploy. **If it doesn't:** Default is unsafe; escalate to Bicep `Microsoft.Network/networkSecurityGroups@... { properties: { ... } }` without `securityRules` declared at all (separate top-level managed-rules collection). This spike is part of T13 acceptance.

---

## Dispatch Plan

Default-profile parallel dispatch where Writes are disjoint. Sequential mode where coupled.

| Task ID | Depends on | Writes (concrete file paths)                                                |
| ------- | ---------- | --------------------------------------------------------------------------- |
| T01     | —          | `Caddyfile`                                                                 |
| T02     | T01        | `tests/infra/test_caddyfile.sh`                                             |
| T03     | T01        | `docker-compose.prod.yml` (caddy service block + volumes)                   |
| T04     | T03        | `docker-compose.prod.yml` (backend.ports + frontend.ports + header comment) |
| T05a    | —          | `scripts/deploy-on-vm.sh` (header + stub only)                              |
| T05b    | T05a       | `scripts/deploy-on-vm.sh` (pre-pull body)                                   |
| T05c    | T05b       | `scripts/deploy-on-vm.sh` (pull/up/probes/rollback body)                    |
| T05d    | T05c       | `tests/infra/test_deploy_on_vm.bats`                                        |
| T06a    | T05a       | `.github/workflows/deploy.yml` (header + triggers + concurrency)            |
| T06b    | T06a       | `.github/workflows/deploy.yml` (sha resolution steps)                       |
| T06c    | T06b       | `.github/workflows/deploy.yml` (azure/login + ssh-agent + known_hosts)      |
| T06d    | T06c       | `.github/workflows/deploy.yml` (scp + ssh + cleanup)                        |
| T06e    | T06d       | `.github/workflows/deploy.yml` (runner-side probes)                         |
| T07     | T06e       | `tests/infra/test_workflow_deploy.sh`                                       |
| T08     | —          | `scripts/backup-to-blob.sh`                                                 |
| T09     | —          | `docs/runbooks/slice-3-rehearsal.md`                                        |
| T10     | T05c, T06e | `docs/runbooks/slice-3-first-deploy.md`                                     |
| T11     | T05c, T06e | `docs/prds/deploy-pipeline-ssh-deploy-and-first-deploy.md`                  |
| T12     | All above  | `docs/CHANGELOG.md`                                                         |
| T13     | —          | `infra/main.bicep` (NSG → child-resource refactor)                          |
| T14     | T13        | `infra/main.bicep` (Network Contributor on NSG for ghOidcMi)                |
| T15     | T13, T14   | `.github/workflows/reap-orphan-nsg-rules.yml`                               |
| T16     | T13–T15    | `docs/decisions/deploy-ssh-jit.md`, `docs/runbooks/deploy-pipeline.md`      |

**Note:** T03 + T04 both modify `docker-compose.prod.yml`. T04 depends on T03 and serializes. T05a → T05b → T05c → T05d all modify `scripts/deploy-on-vm.sh` and serialize. T06a → T06b → T06c → T06d → T06e all modify `.github/workflows/deploy.yml` and serialize.

Sequential mode: this is a tightly-coupled CI/infra slice. Most tasks share files or interface contracts. Dispatch one subagent at a time. Concurrency = 1.

---

## Implementation Notes

### Why Caddy in compose, not as a systemd unit?

Single source of truth for service lifecycle (one `docker compose ps` shows everything). Compose's `depends_on: backend: service_healthy` is cleaner than a systemd `Wants=` chain. Caddy's auto-LE works inside a container with named volumes. The host-port publish (80/443 → caddy:80/443) is the only host-network surface Caddy needs.

### Why explicit service list to `up -d --wait`?

Two reasons. (1) Belt-and-braces: even though `broker` services are profile-gated and won't start without `COMPOSE_PROFILES=broker`, listing only default-profile services in the CLI guarantees no accidental broker activation if a future contributor changes a `profiles: ["broker"]` line. (2) `--wait` only waits on listed services + their `depends_on` transitive closure, so the wait completes when the default profile is healthy without needing to enumerate every service the broker profile depends on (postgres, redis are shared).

### Why `handle` not `handle_path`?

Backend serves under `/api/v1/...` (canonical project convention per `.claude/rules/api-design.md`). `handle_path /api/*` strips the `/api` prefix before proxying — so `https://platform.marketsignal.ai/api/v1/auth/me` would route as `/v1/auth/me` to backend, which returns 404. `handle /api/*` preserves the prefix → backend sees `/api/v1/auth/me` → 401 (auth required, the expected response). This is the difference between "user gets 404 for every API call" and "user gets the actual API response." Comment in Caddyfile makes this explicit so a future contributor doesn't "simplify" by switching.

### Why VM MI for ACR login (not CI-token-through-stdin)?

Research §6: Slice 1 already grants the VM's system-assigned MI the AcrPull role on the ACR. `az login --identity && az acr login --name <short>` on the VM writes the docker credential helper directly. The PRD's original "mint token in CI, pipe over SSH stdin" pattern is a workaround for environments where the deploy target has no MI — the VM has one, so we use it. Net: simpler workflow, fewer steps, no token in process arguments / stdin transit, narrower attack surface.

### What happens on re-deploy of the same SHA?

Idempotent. `/run/msai-images.env` rewritten with same value. `msai-render-env.service` re-runs (refreshes KV — no-op if KV unchanged). `docker compose pull` is no-op if image digest unchanged. `docker compose up -d --wait` no-op for healthy services. Probes run and pass. Total time: ~30-60s steady state.

### What about active live-trading sessions?

Slice 3 explicitly does NOT touch the `broker` profile. The deploy-on-vm.sh service list excludes `live-supervisor` and `ib-gateway`. If an operator has manually brought up the broker profile (`COMPOSE_PROFILES=broker docker compose ... up -d`), Slice 3's deploy will NOT recreate or restart those containers — they keep running. Slice 4 adds a hard refusal gate based on active `live_deployments` rows.

### Rollback bound: 1 step

Each deploy records the current `/run/msai-images.env` to `.last-good.env` BEFORE writing the new one. Rollback restores `.last-good.env` and re-runs pull + up. We do NOT keep an N-deep history — that's a Slice 4+ concern. Manual N-step rollback is `gh workflow run deploy.yml -f git_sha=<older-sha>` which is the explicit-rollback path (UC-2).

### Failure mode coverage

`FAIL_<X>` markers at every phase boundary. Workflow log greps for these for fast triage. Specifically: any FAIL*PROBE*\* triggers automatic 1-step rollback; FAIL_ENV / FAIL_AZ_LOGIN / FAIL_ACR_LOGIN / FAIL_RENDER_ENV / FAIL_CADDY_VALIDATE happen BEFORE any state change, so no rollback needed (deploy fails fast). FAIL_PULL / FAIL_MIGRATE: state changed (new image pulled or migrate ran); rollback re-pins to last-good and tries again.

### What if rollback also fails?

`FAIL_ROLLBACK_BROKEN`. Manual intervention. Runbook: SSH to VM, `cat /run/msai-images.last-good.env`, manually `docker compose pull <last-good>` and `up -d --wait`. If that doesn't work, restore from Hawk's-gate backup. The slicing verdict deliberately accepts this risk for Slice 3; Slice 4 ops/observability adds alerting on `FAIL_ROLLBACK_BROKEN` so the operator is paged.
