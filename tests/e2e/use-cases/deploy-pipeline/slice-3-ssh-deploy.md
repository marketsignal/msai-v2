# E2E Use Cases — Deploy Pipeline Slice 3 (SSH deploy + first prod deploy)

These use cases graduated from `docs/plans/2026-05-10-deploy-pipeline-ssh-deploy-and-first-deploy.md` after the Contrarian's-gate rehearsal-RG smoke + first-deploy passed. They live here as permanent regression tests for any future change touching `deploy.yml`, `deploy-on-vm.sh`, the Caddyfile, or the NSG-mutation pattern.

**Project type:** fullstack (API + UI), but these are operational/CI use cases — not testable from `verify-e2e` against a dev stack. They run during Slice 3+ rehearsals (Contrarian's gate) and post-merge first-deploy (operator gate).

## UC-1: Deploy a known-good SHA via workflow_dispatch (rehearsal RG)

**Interface:** CLI (`gh workflow run`) + API (curl probes through Caddy) + UI (frontend root probe)

**Setup:** Operator has provisioned the rehearsal RG and seeded its KV per `docs/runbooks/slice-3-rehearsal.md` §1–4. Slice 2 image pair `<sha7>` exists in ACR. `VM_SSH_PRIVATE_KEY`, `VM_SSH_KNOWN_HOSTS` populated for the rehearsal VM.

**Steps:**

1. `gh workflow run deploy.yml -f git_sha=<sha7> -f resource_group=msaiv2-rehearsal-<date>`
2. `gh run watch <run-id>` until completion
3. Probe `curl -sf https://platform-rehearsal.marketsignal.ai/health`
4. Probe `curl -sf https://platform-rehearsal.marketsignal.ai/ready`
5. Probe `curl -sI https://platform-rehearsal.marketsignal.ai/`
6. `openssl s_client -connect platform-rehearsal.marketsignal.ai:443 -servername platform-rehearsal.marketsignal.ai </dev/null 2>/dev/null | openssl x509 -noout -issuer`
7. `curl -sI -o /dev/null -w '%{http_code}' https://platform-rehearsal.marketsignal.ai/api/v1/auth/me`

**Verify:**

- Run conclusion: success
- Step 3, 4: HTTP 200
- Step 5: HTTP 200, `content-type: text/html`
- Step 6: issuer contains "Let's Encrypt"
- Step 7: HTTP 401 (NOT 404 — proves Caddy `handle /api/*` preserves the prefix)

**Persist:** Re-run probes after 60s — same results.

## UC-2: Rollback to a previous SHA (rehearsal RG)

**Interface:** CLI + API

**Setup:** UC-1 completed with `<sha-A>`. A second image `<sha-B>` exists in ACR.

**Steps:**

1. `gh workflow run deploy.yml -f git_sha=<sha-B>` against rehearsal RG
2. Wait for success; confirm UC-1 probes still pass
3. `gh workflow run deploy.yml -f git_sha=<sha-A>` (rollback)
4. Wait for success; SSH to rehearsal VM and `sudo docker compose --project-name msai ps --format json | jq '.[] | select(.Service=="backend") | .Image'`

**Verify:** Step 4 returns `<acr>/msai-backend:<sha-A>` (rolled back).

**Persist:** N/A — this IS the persistence test for the rollback path.

## UC-3: Deliberate failure triggers automatic rollback

**Interface:** CLI + API

**Setup:** Rehearsal RG has `<sha-A>` deployed and healthy. Stage `<sha-broken>` — easiest path: build a backend image that exits 1 immediately on startup (CMD `false`) or that fails the `/health` probe.

**Steps:**

1. `gh workflow run deploy.yml -f git_sha=<sha-broken>` against rehearsal RG
2. `gh run watch` — observe failure

**Verify:**

- Run conclusion: failure
- `gh run view --log` contains `FAIL_PROBE_HEALTH` followed by `FAIL_ROLLBACK_OK`
- `docker compose --project-name msai ps` on the VM shows backend running `<sha-A>` (rolled back)
- UC-1 probes pass against `<sha-A>`

**Persist:** Re-run UC-1 probes after 60s — `<sha-A>` still running.

## UC-4: Backup-to-Blob (Hawk's gate)

**Interface:** CLI (`scripts/backup-to-blob.sh` on VM) + Azure CLI (operator side)

**Setup:** Rehearsal (or prod first-deploy pre-flight) RG has Postgres up but empty. VM has system-assigned MI with Storage Blob Data Contributor.

**Steps:**

1. SSH to VM
2. `sudo /opt/msai/scripts/backup-to-blob.sh`
3. From operator's machine: `az storage blob list --auth-mode login --account-name <bicep-output-storage-acct> --container-name msai-backups --prefix backup-$(date -u +%Y%m%d) --query '[].name' -o tsv`

**Verify:** Step 3 returns ≥1 blob whose name starts with today's UTC date.

**Persist:** Re-run step 3 — same blob still present.

## UC-5: First real prod deploy (post-merge, operator gate)

**Interface:** CLI + API + UI

**Setup:** PR merged, operator pre-flight (PRD §10) complete.

**Steps:** UC-1 procedure run against `msaiv2_rg` (default — no `resource_group` override). 5 probes against `https://platform.marketsignal.ai/`.

**Verify:** Same as UC-1 against prod hostname. Evidence captured in `docs/CHANGELOG.md`.

**Persist:** Re-probe 5 minutes after deploy — same results.

## UC-6: Caddyfile validation catches typos before pull

**Interface:** CLI (direct VM invocation, NOT deploy.yml)

**Setup:** Stage a deliberate Caddyfile typo on the VM (e.g., replace `handle /api/*` with `handel /api/*`).

**Steps:** `sudo bash /tmp/deploy-on-vm.sh <some-sha> /tmp/some-env.env` directly on the VM.

**Verify:** Script exits with `FAIL_CADDY_VALIDATE` BEFORE running `docker compose pull`. Existing Caddy container keeps running. `docker compose --project-name msai ps caddy --format json` shows pre-existing container running.

**Persist:** N/A.

## UC-7: Reaper deletes orphan transient SSH rules

**Interface:** CLI

**Setup:** Manually create a `gha-transient-test-orphan-1` NSG rule, then wait 31 minutes (or use a backdated `created` timestamp via Activity Log filter — easiest: just wait).

**Steps:** Wait for the next 15-min reaper cron, OR `gh workflow run reap-orphan-nsg-rules.yml`.

**Verify:** `az network nsg rule list -g $RG --nsg-name $NSG_NAME --query "[?name=='gha-transient-test-orphan-1']"` returns empty.

**Persist:** Re-run query — still empty (rule remains deleted).
