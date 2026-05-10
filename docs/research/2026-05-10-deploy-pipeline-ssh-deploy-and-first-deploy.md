# Research: deploy-pipeline-ssh-deploy-and-first-deploy

**Date:** 2026-05-10
**Feature:** Slice 3 of 4 — `.github/workflows/deploy.yml` (OIDC + SSH-from-runner) + `scripts/deploy-on-vm.sh` + Caddyfile + `caddy` compose service + `scripts/backup-to-blob.sh` update. First real prod deploy.
**Researcher:** research-first agent

> **Scope note.** Pure infra/CI/ops. No `package.json` / `pyproject.toml` deltas. The "external libraries/APIs" researched are: GH Actions SSH patterns, docker-compose-plugin v2 wait/profile semantics, Caddy 2 (Caddyfile + reverse-proxy + automatic HTTPS), Azure CLI storage CLI with MI, GH Actions `workflow_run` trigger, and ACR login via system-assigned MI. 6 priority topics from PRD §11.

---

## Libraries / APIs Touched

| Surface                               | Pinned form (PRD/repo)                           | Latest stable (2026-05-10)      | Breaking changes vs assumed shape                                                          | Source                                                                                                                                                    |
| ------------------------------------- | ------------------------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `appleboy/ssh-action`                 | unpinned (PRD assumption)                        | **v1.2.4**                      | Active; supports `proxy_*`, `script_path`, key_path, host fingerprint                      | [GitHub — appleboy/ssh-action releases](https://github.com/appleboy/ssh-action/releases) (2026-05-10)                                                     |
| `webfactory/ssh-agent`                | unpinned                                         | **v0.9.1**                      | Active; recommended for multi-step SSH                                                     | [GitHub — webfactory/ssh-agent](https://github.com/webfactory/ssh-agent) (2026-05-10)                                                                     |
| Docker Compose plugin                 | v2.x on VM                                       | v2.40+                          | `--wait` honors one-shot `restart: no` services that exit 0; bug #11638 closed             | [docker/compose#11638](https://github.com/docker/compose/issues/11638) (2026-05-10)                                                                       |
| Caddy 2                               | `caddy:2-alpine` (PRD)                           | **v2.10.x**                     | Caddyfile syntax stable; `handle /api/*` + `handle` (catch-all) is canonical               | [Caddy — Common Patterns](https://caddyserver.com/docs/caddyfile/patterns) (2026-05-10)                                                                   |
| Azure CLI `az storage blob`           | latest on VM                                     | 2.71+                           | `--auth-mode login` with system-assigned MI works; `az login --identity` pre-step required | [MS Learn — Authorize blob with Entra](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-data-operations-cli) (2026-05-10)                  |
| GH Actions `workflow_run` trigger     | (Slice 3 introduces)                             | unchanged                       | `types: [completed]` fires on ALL conclusions — explicit `if:` gate required               | [GitHub Docs — Events that trigger workflows](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows) (2026-05-10)              |
| ACR auth via system-assigned MI on VM | `az acr login --expose-token` (Slice 2 CI shape) | `az acr login` (no token) on VM | Cleaner than CI shape — VM uses cached docker config from MI                               | [MS Learn — ACR auth with MI](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity) (2026-05-10) |

---

## Per-Topic Analysis

### 1. SSH-from-runner pattern

**Findings:**

1. **Two well-maintained options in 2026:** `appleboy/ssh-action@v1.2.4` (single-step exec with built-in script transport) and `webfactory/ssh-agent@v0.9.1` (loads key into ssh-agent, then raw `ssh`/`scp` commands). Both actively maintained; no deprecations announced.
2. **`appleboy/ssh-action` strengths:** ed25519 key support, single-step ergonomic, built-in `script_path` for transferring + executing, masking, proxy support. **Weakness:** the script body lives inline in YAML — long scripts become unmanageable; `scp` is a separate action (`appleboy/scp-action`).
3. **`webfactory/ssh-agent` strengths:** key is loaded into agent so all subsequent `ssh`/`scp`/`rsync` steps work; closer to bash idioms; better for multi-command flows like "scp script, ssh exec, scp logs back."
4. **`StrictHostKeyChecking=yes` is the secure default.** The known_hosts entry is NOT a secret — the SSH host key is public. Recommended pattern: capture during VM provisioning (Slice 1 acceptance) via `ssh-keyscan -t ed25519 <ip>` and persist as a GitHub Variable (`VM_SSH_KNOWN_HOSTS`). Inline `ssh-keyscan` at runtime is vulnerable to MITM and **must be avoided**.
5. **ed25519 over RSA in 2026.** Smaller, faster, no known weaknesses. Match Slice 1 cloud-init's `authorized_keys` injection.

**Sources:**

1. [GitHub — appleboy/ssh-action releases](https://github.com/appleboy/ssh-action/releases) — accessed 2026-05-10
2. [GitHub — webfactory/ssh-agent README](https://github.com/webfactory/ssh-agent) — accessed 2026-05-10 ("known_hosts file… SSH host key is not secret and can safely be committed")
3. [GitHub Docs — Error: Host key verification failed](https://docs.github.com/en/authentication/troubleshooting-ssh/error-host-key-verification-failed) — accessed 2026-05-10

**Design impact:**

**Adopt `webfactory/ssh-agent@v0.9.1`** for `deploy.yml` because we need both `scp` (transfer `deploy-on-vm.sh`) and `ssh` (execute, capture exit code) in the same job. Pattern:

```yaml
- uses: webfactory/ssh-agent@v0.9.1
  with:
    ssh-private-key: ${{ secrets.VM_SSH_PRIVATE_KEY }}

- name: Trust VM host key
  run: |
    mkdir -p ~/.ssh
    echo "${{ vars.VM_SSH_KNOWN_HOSTS }}" >> ~/.ssh/known_hosts
    chmod 600 ~/.ssh/known_hosts

- name: Push deploy script + execute
  env:
    VM_USER: ${{ vars.VM_SSH_USER }}
    VM_IP: ${{ vars.VM_PUBLIC_IP }}
    GIT_SHA: ${{ needs.compute-sha.outputs.sha }}
  run: |
    scp scripts/deploy-on-vm.sh "$VM_USER@$VM_IP:/tmp/deploy-on-vm-${GITHUB_RUN_ID}.sh"
    ssh "$VM_USER@$VM_IP" "sudo bash /tmp/deploy-on-vm-${GITHUB_RUN_ID}.sh '$GIT_SHA' && rm /tmp/deploy-on-vm-${GITHUB_RUN_ID}.sh"
```

**Test implication:**

Unit test for `deploy.yml`: `actionlint` parses; the `webfactory/ssh-agent` step appears once before any ssh/scp; `VM_SSH_KNOWN_HOSTS` is appended to `~/.ssh/known_hosts` before the first `ssh` command. Acceptance smoke (rehearsal RG): full SSH round-trip succeeds with `StrictHostKeyChecking=yes` (the runner default once known_hosts is populated). A second smoke deliberately omits `VM_SSH_KNOWN_HOSTS` to confirm strict-host fails closed.

---

### 2. `docker compose pull` + `up -d --wait` semantics on v2.40+

**Findings:**

1. **`--wait` waits for "running or healthy" on services that have `healthcheck:` or for "started" otherwise.** For one-shot services (`restart: no`), it now correctly waits for **completed (exit 0)** state — bug #11638 fix landed in compose v2.30+. Our `migrate` service (`restart: no`, runs `alembic upgrade head`, exits 0) is the canonical case.
2. **`--wait-timeout <sec>`** is a hard cap. If any service has not reached terminal-good state within the window, `up` exits non-zero. Recommended ≥ 300s for cold start (Postgres healthcheck is ~10s × 5 retries; backend startup is ~30s; migrate is ~10–60s for our schema).
3. **Subset-of-services enumeration.** `docker compose -f compose.yml up -d --wait <svc1> <svc2> ...` ONLY brings up the listed services (and their `depends_on`). Profiles do NOT auto-activate from a service list — profile-tagged services are skipped unless `--profile` or `COMPOSE_PROFILES` is set. **This is exactly what Slice 3 needs**: the `broker`-profile services (`live-supervisor`, `ib-gateway`) stay down even if listed implicitly via depends_on, because they're profile-gated.
4. **`--env-file` is repeatable (multi-`--env-file`).** Compose plugin v2.24+ accepts multiple `--env-file` flags; **later files override earlier ones** (last-wins) for keys that collide. Confirmed in compose docs and changelog. Order: `--env-file /run/msai.env --env-file /run/msai-images.env` — image SHAs win over rendered KV env (but they don't collide since they own different keys).
5. **`docker compose pull` honors `~/.docker/config.json` credentials** for ACR. After `az acr login` (using the VM's system-assigned MI — see topic 6) the credentials are cached and `pull` succeeds without re-auth.
6. **One-shot + long-running mixed in same `up --wait`:** compose treats them correctly — long-running must be `running` (or `healthy` if healthcheck), one-shot must be `exited 0`. **Order of declaration in the CLI does not matter**; compose schedules per `depends_on`.

**Sources:**

1. [Docker Docs — `docker compose up`](https://docs.docker.com/reference/cli/docker/compose/up/) — accessed 2026-05-10
2. [docker/compose#11638 — `--wait` with `restart: no`](https://github.com/docker/compose/issues/11638) — accessed 2026-05-10 (closed; behavior verified)
3. [Ken Muse — Waiting for Docker Compose Up](https://www.kenmuse.com/blog/waiting-for-docker-compose-up/) — accessed 2026-05-10

**Design impact:**

`deploy-on-vm.sh` enumerates the default-profile services explicitly so `broker` profile stays untouched even with shared volumes:

```bash
docker compose \
  -f /opt/msai/docker-compose.prod.yml \
  --env-file /run/msai.env \
  --env-file /run/msai-images.env \
  pull postgres redis backend frontend backtest-worker research-worker portfolio-worker ingest-worker

docker compose \
  -f /opt/msai/docker-compose.prod.yml \
  --env-file /run/msai.env \
  --env-file /run/msai-images.env \
  up -d --wait --wait-timeout 300 \
    postgres redis migrate backend backtest-worker research-worker portfolio-worker ingest-worker frontend caddy
```

Caddy is added to the explicit list. `migrate` (one-shot) appears in the same list; `--wait` will wait for its exit-0 alongside healthchecks for the rest.

**Test implication:**

Integration test (rehearsal RG): introduce a deliberate fault (`migrate` references missing alembic head) → `up -d --wait` exits non-zero with classified `FAIL_MIGRATE`. Second test: kill backend mid-startup → `--wait-timeout 300` triggers, `FAIL_PROBE_HEALTH`. Confirm `broker` services do NOT start unless `COMPOSE_PROFILES=broker` is set: `docker compose ps` post-deploy shows only default-profile services.

---

### 3. Caddy 2 in 2026

**Findings:**

1. **Caddyfile syntax is stable.** The 2026 idiom for path-based reverse-proxy with two upstreams uses `handle` blocks (preferred over `route` for ordered, mutex-style routing — first match wins, no fall-through):

   ```caddyfile
   {$MSAI_HOSTNAME} {
       encode gzip zstd

       handle /api/* {
           reverse_proxy backend:8000
       }

       handle {
           reverse_proxy frontend:3000
       }
   }
   ```

2. **Env-var interpolation:** `{$VAR}` (with leading `$`) substitutes at Caddyfile-parse time before any Caddy directive evaluation. Works for hostnames, upstreams, ports. Empty/unset vars become empty strings — fail loudly by validating in `deploy-on-vm.sh` before `docker compose up`.
3. **Automatic HTTPS:** When the site address is a hostname (not `:80` or an IP), Caddy auto-issues from Let's Encrypt or ZeroSSL. **Default LE rate limit:** 5 duplicate certs / domain / week + 50 certs / registered-domain / week. Rehearsal MUST use `platform-rehearsal.marketsignal.ai` (different rate-limit bucket).
4. **Cert state persistence:** Caddy stores ACME state under `/data` (account keys, certs) and runtime config under `/config`. Mount **named volumes** (`caddy_data`, `caddy_config`) — bind mounts work but introduce host-permission friction.
5. **Healthcheck for caddy service:** `wget -qO- http://localhost:2019/config/ || exit 1` against the admin API works but the admin API is bound to `localhost` inside the container. Simpler: `wget -qO- --no-check-certificate https://localhost/health || exit 1` (probes through Caddy itself). For the dependency ordering only, `service_started` is acceptable since `--wait` covers readiness.
6. **`handle` vs `handle_path`:** `handle_path /api/*` strips the matched prefix before proxying; `handle /api/*` preserves it. Our backend serves under `/api/v1/...` so we **must** use `handle` (preserve prefix); using `handle_path` would route `/api/v1/health` → backend's `/v1/health` and 404.
7. **Caddy validation:** `caddy validate --config /etc/caddy/Caddyfile` or `docker compose run --rm caddy caddy validate --config /etc/caddy/Caddyfile` exits non-zero on syntax errors — useful pre-flight before `up -d --wait`.

**Sources:**

1. [Caddy — Common Caddyfile Patterns](https://caddyserver.com/docs/caddyfile/patterns) — accessed 2026-05-10
2. [Caddy — `reverse_proxy` directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — accessed 2026-05-10
3. [Caddy — Caddyfile Concepts (env vars + matchers)](https://caddyserver.com/docs/caddyfile/concepts) — accessed 2026-05-10

**Design impact:**

- Caddyfile at repo root uses `handle /api/*` (NOT `handle_path`) so backend's `/api/v1/...` paths resolve.
- Env-var interpolation: `{$MSAI_HOSTNAME}` at the site-address position. `deploy-on-vm.sh` validates `MSAI_HOSTNAME` is non-empty before `up`.
- Compose service: image `caddy:2-alpine`, named volumes `caddy_data:/data` + `caddy_config:/config`, ports `80:80` and `443:443`, depends_on backend (service_healthy) + frontend (service_started), `restart: unless-stopped`.
- Pre-flight: `deploy-on-vm.sh` runs `docker compose run --rm caddy caddy validate --config /etc/caddy/Caddyfile` before the main `up -d --wait`. Validation failure → exit `FAIL_CADDY_VALIDATE` BEFORE pulling new images, so existing Caddy keeps running.

**Test implication:**

Unit test: a small bash test asserts Caddyfile parses with `caddy validate` against fixture `MSAI_HOSTNAME=test.example.com`. Integration (rehearsal): probe `https://platform-rehearsal.marketsignal.ai/health` returns 200 with LE cert chain (`openssl s_client | openssl x509 -noout -issuer` shows `O=Let's Encrypt`); probe `https://.../api/v1/auth/me` returns 401 (auth error, NOT 404 — proves `/api/*` proxies to backend with prefix preserved). Rate-limit fail-open scenario: deliberately point DNS to wrong IP, confirm Caddy logs `tls.issuance.acme: challenge failed` and the deploy fails with `FAIL_PROBE_TLS`.

---

### 4. `az storage blob` with system-assigned MI on VM

**Findings:**

1. **VM with system-assigned MI: required pre-step is `az login --identity`** (no args needed for system-assigned). After this, the CLI session has an Entra token; subsequent `az storage blob` commands with `--auth-mode login` use it.
2. **`az storage blob upload-batch --auth-mode login --account-name <name> --destination <container> --source <local>`** — works with MI provided the MI has **Storage Blob Data Contributor** on the storage account (Slice 1 already grants this per PRD §3).
3. **`az storage blob list --auth-mode login --account-name <name> --container-name msai-backups --prefix backup-`** — returns JSON; no key/SAS needed.
4. **`--account-name` accepts the bare storage-account name** (e.g. `msaistorageXXX`) — NOT the FQDN. Bicep output `backupsStorageAccount` should be the bare name.
5. **Performance caveat:** `--auth-mode login` is slower than key-auth for large batches (issue #26717 documents 10× slowdown for tens-of-thousands of blobs). For the Hawk's-gate empty-DB dump and small Parquet seed, this is fine. For Slice 4 nightly mirroring of the full Parquet tree, **use `azcopy` (which handles MI natively via `azcopy login --identity` + parallel transfer)** instead of `az storage blob upload-batch`.
6. **Token TTL:** Entra access token from `az login --identity` is ~24h; backup script runs in seconds. No realistic expiration risk.

**Sources:**

1. [MS Learn — Authorize access to blob data with Azure CLI](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-data-operations-cli) — accessed 2026-05-10
2. [MS Learn — Authorize Blob Access with Microsoft Entra ID](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-managed-identity) — accessed 2026-05-10
3. [Azure-cli #26717 — `az storage blob sync` with `--auth-mode login` performance](https://github.com/Azure/azure-cli/issues/26717) — accessed 2026-05-10

**Design impact:**

`scripts/backup-to-blob.sh` (Slice 3 update):

```bash
set -euo pipefail

# Resolve target storage account from last Bicep deploy
STORAGE_ACCT="$(az deployment group show \
  --name "${DEPLOYMENT_NAME:-msai-iac}" \
  --resource-group "${RESOURCE_GROUP:-msaiv2_rg}" \
  --query 'properties.outputs.backupsStorageAccount.value' -o tsv)"

CONTAINER="$(az deployment group show \
  --name "${DEPLOYMENT_NAME:-msai-iac}" \
  --resource-group "${RESOURCE_GROUP:-msaiv2_rg}" \
  --query 'properties.outputs.backupsContainerName.value' -o tsv)"

if [[ -z "$STORAGE_ACCT" || -z "$CONTAINER" ]]; then
  echo "ERROR: Bicep outputs missing — confirm 'az deployment group show' returns backupsStorageAccount + backupsContainerName" >&2
  exit 2
fi

# Ensure MI session
az login --identity --output none

# pg_dump piped to az (no temp file)
pg_dump ... | az storage blob upload \
  --auth-mode login \
  --account-name "$STORAGE_ACCT" \
  --container-name "$CONTAINER" \
  --name "backup-$(date -u +%Y%m%dT%H%M%SZ).sql.gz" \
  --file /dev/stdin
```

Slice 4 will replace the Parquet-tree mirroring step with `azcopy --recursive`.

**Test implication:**

Integration: run script against rehearsal RG's storage account; `az storage blob list --auth-mode login --account-name <acct> --container-name msai-backups --prefix backup-` returns ≥1 blob with today's UTC date in the name. Negative test: revoke the MI's role assignment temporarily (or run on a VM without Storage Blob Data Contributor) → script fails with `AuthorizationFailure` and exit non-zero before pg_dump runs.

---

### 5. GH Actions `workflow_run` trigger

**Findings:**

1. **`workflow_run` fires on ALL completion conclusions** (success, failure, cancelled, skipped, timed_out, action_required, neutral). The trigger does NOT auto-filter on success. **Explicit gate required:**

   ```yaml
   on:
     workflow_run:
       workflows: ["Build and Push Images"]
       types: [completed]
       branches: [main]
     workflow_dispatch:
       inputs:
         git_sha: { required: false, type: string }

   jobs:
     deploy:
       if: ${{ github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success' }}
       runs-on: ubuntu-24.04
   ```

2. **Branch filter on `workflow_run`** filters by the _triggering_ workflow's branch (the build workflow's branch), not the deploy workflow's branch. `branches: [main]` here means "run deploy when the build ran on main."
3. **`workflow_dispatch` coexistence is fine.** The job-level `if:` covers both cases — the `github.event_name` branch ensures dispatch always runs (regardless of conclusion field, which doesn't exist on dispatch events).
4. **Concurrency across triggers:** `concurrency: group: deploy-msai, cancel-in-progress: false` applies regardless of trigger source. A `workflow_dispatch` rollback while a `workflow_run`-triggered deploy is in-flight will queue (not cancel) — correct behavior per PRD AC.
5. **`workflow_run` runs from the default-branch version of the workflow file**, not from the SHA of the triggering build. This means deploy.yml changes in feature branches don't take effect until merge — same constraint as Slice 2.
6. **`workflow_run` is sometimes silently skipped** (community discussion #21090) when the workflow file has just been added/modified in the same commit that produced the build. Workaround: trigger the first deploy manually via `workflow_dispatch` for the merge commit, then auto-trigger thereafter.

**Sources:**

1. [GitHub Docs — Events that trigger workflows: workflow_run](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run) — accessed 2026-05-10
2. [GitHub community #102876 — workflow_run triggered only on success](https://github.com/orgs/community/discussions/102876) — accessed 2026-05-10
3. [GitHub community #21090 — workflow_run sometimes skipped](https://github.com/orgs/community/discussions/21090) — accessed 2026-05-10

**Design impact:**

Workflow shape (PRD §5 US-001 AC alignment):

```yaml
on:
  workflow_run:
    workflows: ["Build and Push Images"]
    types: [completed]
    branches: [main]
  workflow_dispatch:
    inputs:
      git_sha:
        required: false
        type: string
        description: "7-char SHA to deploy (rollback). Defaults to triggering commit."

permissions:
  id-token: write
  contents: read

concurrency:
  group: deploy-msai
  cancel-in-progress: false

jobs:
  deploy:
    if: ${{ github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success' }}
    runs-on: ubuntu-24.04
    steps:
      - name: Validate git_sha input
        if: ${{ github.event_name == 'workflow_dispatch' && inputs.git_sha != '' }}
        run: |
          [[ "${{ inputs.git_sha }}" =~ ^[0-9a-f]{7}$ ]] || { echo "git_sha must be 7 hex chars"; exit 1; }
      ...
```

The first-deploy-after-merge edge case (workflow_run silently skipped) is documented in the runbook: operator triggers Slice 3's first invocation via `workflow_dispatch` after merge.

**Test implication:**

Smoke A: push a no-op commit to main → build-and-push.yml runs → deploy.yml auto-fires → `gh run view` shows `triggering_actor` is the build, `event` is `workflow_run`. Smoke B: `gh workflow run deploy.yml -f git_sha=abc1234` → manual run, deploys the supplied SHA. Smoke C (negative): force build to fail → deploy.yml fires but the `if:` short-circuits the deploy job to skipped — confirm via `gh run view --json jobs`.

---

### 6. ACR docker login on VM via system-assigned MI

**Findings:**

1. **Yes — `az acr login --name <acrShortName>` works on a VM with system-assigned MI assigned an AcrPull role on the registry.** Pre-step: `az login --identity` (no args). Then `az acr login --name <acrShortName>` writes the auth helper into `~/.docker/config.json` and subsequent `docker pull` succeeds. **No `--expose-token`, no out-of-band token transfer, no SSH stdin gymnastics.**
2. **Token TTL = 3 hours.** Refresh is automatic when `az acr login` is re-run; for our deploy (~5 min wall clock), one `az acr login` at top of `deploy-on-vm.sh` is sufficient.
3. **MI has AcrPull, NOT AcrPush.** This is correct — VM only pulls; CI pushes. Slice 1 grants AcrPull on VM-MI; Slice 2 grants AcrPush on GH-OIDC-MI. Two distinct principals.
4. **Compared to Slice 2 CI shape** (`az acr login --expose-token` → token through `docker/login-action`): the CI shape exists because GH runners do not have an Azure MI — they federate via OIDC and need the token-extraction dance. **The VM has an actual MI assignment**, so the simpler `az acr login --name` path is correct and preferred.
5. **`--name` expects the short ACR name**, not the FQDN — same gotcha as Slice 2 (per Slice 2 brief Open Risk #9). Reuse the existing `ACR_NAME` GH Variable.
6. **What if MI propagation lags?** First `az acr login` after fresh role assignment may 403; retry after 60s. Same propagation note as Slice 2.

**Sources:**

1. [MS Learn — Managed Identity Authentication for ACR](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication-managed-identity) — accessed 2026-05-10. Quote: _"after signing in this way, your credentials are cached for subsequent docker commands."_
2. [MS Learn — Azure Container Registry authentication options](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication) — accessed 2026-05-10
3. [Shawn.L — Access ACR using Azure Managed Identity](https://shuanglu1993.medium.com/access-the-azure-container-registry-using-azure-managed-identity-programatically-d203ca17170b) — accessed 2026-05-10

**Design impact:**

**Simplifies `deploy.yml` materially.** Original PRD shape was: GH runner mints `az acr login --expose-token`, pipes the token over SSH stdin to VM, VM `docker login`s with the token. **Replace with:** GH runner SSHs to VM, VM runs `az login --identity && az acr login --name $ACR_NAME` itself. No token transit through CI. `deploy-on-vm.sh` owns the ACR auth step:

```bash
# At top of deploy-on-vm.sh, after ENV validation
az login --identity --output none
az acr login --name "${MSAI_ACR_NAME:?}"
```

`MSAI_ACR_NAME` is passed as the second positional arg from `deploy.yml` (or read from `/run/msai-images.env`).

**This deviates from PRD §3 ("fetches a one-shot ACR access token (handed to the VM via SSH stdin)").** Recommend updating PRD during plan-review Phase 3 to reflect simpler MI-direct path. Documented in Open Risks.

**Test implication:**

Integration (rehearsal): `deploy-on-vm.sh` runs `az acr login --name $ACR_NAME` → exit 0 + `docker pull <ACR_FQDN>/msai-backend:<sha>` succeeds. Negative test: temporarily remove AcrPull role from VM MI, re-run → `az acr login` fails with `AuthorizationFailed`; deploy exits `FAIL_PULL` with remediation hint pointing to Slice 1's `vmAcrPullAssignment`.

---

## Not Researched (with justification)

- **`docker/login-action` on VM:** unnecessary — `az acr login` writes the docker credential helper directly. Topic 6 settles this.
- **Caddy plugins (e.g., LE DNS-01, custom matchers):** Slice 3 needs only HTTP-01 challenge (port 80 inbound is in NSG). DNS-01 is Phase 2 ops if we ever go private.
- **`docker compose --profile broker` deploys:** explicit Non-Goal per PRD §2. Slice 4.
- **`scp`-action vs SSH-action:** webfactory/ssh-agent + raw `scp` covers it; no third-party `scp-action` needed.
- **Caddy admin API security:** bound to `localhost` inside the container by default; not exposed. Acceptable for Slice 3.
- **Probe retry logic in `deploy-on-vm.sh`:** standard bash `for i in {1..30}; do curl -sf ... && break; sleep 2; done` pattern; not a research topic.

---

## Open Risks (consolidated)

1. **PRD §3 specifies "fetches a one-shot ACR access token (handed to the VM via SSH stdin)" — research finds the simpler MI-direct path (`az login --identity && az acr login --name`).** Recommend updating PRD + plan to use VM-MI directly. No token transit through CI = strictly simpler + strictly more secure (no token-in-stdin attack surface). Discuss in plan-review Phase 3.

2. **`workflow_run` silently-skipped edge case** on the very first run after merging Slice 3. Document in runbook: operator triggers first deploy via `gh workflow run deploy.yml -f git_sha=<merge-sha>` instead of waiting for auto-trigger.

3. **`handle` vs `handle_path` mistake = silent 404 storm.** If a future contributor changes the Caddyfile to `handle_path /api/*`, backend routes 404. Caddyfile must include a comment explaining the prefix-preservation requirement; consider an integration test that asserts `/api/v1/auth/me` returns 401 (unauth, but routed) rather than 404.

4. **LE rate-limit during rehearsal.** PRD §9 already mentions; reinforced by topic 3. Hard requirement: rehearsal uses `platform-rehearsal.marketsignal.ai` (different rate-limit bucket from `platform.marketsignal.ai`). If both share a registered domain, the 50/week registered-domain limit can still bite — verify with `crt.sh` before the rehearsal that no recent issuance bursts have happened on `marketsignal.ai`.

5. **`--env-file` ordering must be deliberate.** `--env-file /run/msai.env --env-file /run/msai-images.env` — image-SHA file is LAST so it always wins on key collision. PRD's US-001 example already has this order; preserve it.

6. **Caddy validation must run BEFORE `docker compose pull`** in `deploy-on-vm.sh`. If validation runs after pull, a Caddyfile typo wastes pull bandwidth and lets us into a partial deploy. Order: (1) ENV validation → (2) Caddyfile validate → (3) `docker compose pull` → (4) `up -d --wait` → (5) probes.

7. **`--auth-mode login` slowness on large `upload-batch`** (topic 4). Slice 3's Hawk's-gate dump is fine (one file). Slice 4 nightly Parquet mirror MUST migrate to `azcopy` to avoid hours-long backups. Flag in Slice 3 PR description as deferred Slice-4 carry-over.

8. **`az login --identity` failure modes on first deploy.** If the VM was provisioned without `Microsoft.ManagedIdentity` extension, `az login --identity` returns `MSI_ENDPOINT not available`. Mitigation: Slice 1 cloud-init must verify MI is reachable via `curl -sH 'Metadata: true' 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F'` before reporting cloud-init success. Re-verify this is in Slice 1's acceptance smoke before Slice 3 first-deploy.

9. **PRD's `XINFO GROUPS msai:live:commands` probe is soft on default profile** (per PRD §6 D-1). Reinforce in `deploy-on-vm.sh`: check stream existence with `redis-cli EXISTS msai:live:commands`; if 0, log "WARN: live-supervisor stream not yet created — OK for default-profile deploy" and continue. Do NOT hard-fail.
