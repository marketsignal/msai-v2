# Local and Azure runtime configuration assessment

Date: 2026-10-03. Source revision: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`. This is the runtime-configuration companion to [live safety](live-safety.md) and [product/operations](product-operations.md).

**The root development and production stacks share application code but have materially different startup, authentication, storage, and broker behavior.** A default development `up` is too broad for passive inspection: it starts job workers and the API itself starts broker probes. The parent audit instead started the explicit four-service local application with guarded broker/vendor settings and intentionally preserved existing local data. No assumption is made that a paper account exists.

This report's author inspected source, scripts, and documentation only, plus `docker compose version` (installed CLI: `v2.40.3-desktop.1`). No services, database/Redis queries, broker/vendor connections, Azure commands, secret retrieval, or migrations were executed by this subtask. The local runtime observations below were supplied by the parent audit. Remote commands are proposed read-only evidence collection, not completed checks.

## Development versus production

| Boundary | Root development configuration | Root production configuration | Practical implication |
| --- | --- | --- | --- |
| Images and code | Builds `backend/Dockerfile.dev` and `frontend/Dockerfile.dev`; mounts current source/config (`docker-compose.dev.yml:13`, `:105`, `:361`). Backend reloads; frontend runs Next development server. | Prebuilt SHA-tagged backend/frontend images (`docker-compose.prod.yml:98`, `:121`, `:482`); strategies and migrations baked into backend image (`backend/Dockerfile:16`). | Current checkout and actual deployed image are different evidence. Compare image IDs/digests, not just a Git checkout on the VM. |
| Default services | PostgreSQL, Redis, API, frontend, backtest/research/portfolio/ingest workers, plus `job-watchdog` (`docker-compose.dev.yml:146`, `:196`). | Adds one-shot migration and Caddy; workers have no separate `job-watchdog` container. Explicit routine-deploy service list at `scripts/deploy-on-vm.sh:50`. | Default startup executes work; select services explicitly for inspection. |
| Migrations | No migrate service. API waits for PostgreSQL and Redis health, not schema readiness (`docker-compose.dev.yml:126`). | `migrate` runs `alembic upgrade head`; API and every worker wait for success (`docker-compose.prod.yml:98`, `:208`, `:257`). | A new local database requires deliberate schema initialization; production `up` can migrate automatically. Neither is a read-only inspection command. |
| Storage | Fixed `msai_postgres_data`, repository `./data`, source-mounted strategies (`docker-compose.dev.yml:22`, `:111`, `:400`). | Same fixed database volume name, plus `msai_app_data`, gateway state, Caddy state (`docker-compose.prod.yml` volumes block). | `--project-name` alone does not isolate the database. Local startup may intentionally reuse prior state, as in this audit. Never use `down -v` as an inspection cleanup step. |
| Redis | No explicit persistent volume/AOF setup (`docker-compose.dev.yml:85`). | Same absence (`docker-compose.prod.yml:80`). | Container replacement/state loss is distinct from network interruption; see live-safety L5. Do not recreate Redis simply to obtain a clean audit environment. |
| Ports | Host 3300, 8800, 5433, 6380, with no explicit loopback address (`docker-compose.dev.yml:68`, `:88`, `:108`, `:366`). | API/frontend loopback 8000/3000, PostgreSQL/Redis unpublished, Caddy on 80/443 (`docker-compose.prod.yml:123`, frontend/Caddy blocks). | A localhost probe does not establish loopback-only exposure. Inspect actual Docker port bindings. Dev authentication defaults are inappropriate for an externally reachable shared host. |
| API authentication | `ENVIRONMENT=development`, default `MSAI_API_KEY=msai-dev-key` passed to API and browser (`docker-compose.dev.yml:29`, `:384`). | Entra tenant/client, CORS, strong report signing secret, Key Vault URI required; API key remains optional (`docker-compose.prod.yml:142`). | Documented local API-key mode is usable; it does not prove production SSO or partner roles. |
| Frontend configuration | `NEXT_PUBLIC_API_URL=http://localhost:8800`, runtime development key (`docker-compose.dev.yml:382`). | Public Entra/API values baked at image build; runtime API URL is only a server-side fallback (`frontend/Dockerfile:23`, `docker-compose.prod.yml` frontend comments). | Changing production container environment does not repair a previously built browser bundle's auth/API destination. |
| Backend processes | One reload server (`backend/Dockerfile.dev:9`). | Two Uvicorn workers, non-root (`backend/Dockerfile:49`). | Startup background tasks and process-local caches exist per API process. Source-only local behavior is not proof of multiprocess convergence. |
| Broker services | `broker` profile contains supervisor and primary gateway; `broker-hvp` adds second gateway. Gateway uses floating `stable` image. HVP default trading mode is live (`docker-compose.dev.yml:213`, `:250`, HVP block). | Primary gateway image pinned `10.43.1c`; supervisor and gateway behind `broker` profile (`docker-compose.prod.yml` broker blocks). | Neither profile names nor defaults prove the account is a safe test account. Verify actual account/session/mode before any future broker acceptance. |
| Broker order permission | `READ_ONLY_API` defaults to `no`, including HVP. | Also defaults to `no`. | Starting a gateway with credentials can create an order-capable session. Do not start broker profiles for ordinary application inspection. |
| Host automation | Not defined by dev Compose. | Deploy script enables backup and gateway-watchdog timers (`scripts/deploy-on-vm.sh:430`, `:538`). | Runtime lifecycle extends beyond Compose. Inspect timers as well as containers. |

Production's settings validator requires a non-default report signing secret of at least 32 characters (`backend/src/msai/core/config.py:385`). Production credentials-store construction requires Key Vault URI and explicit managed-identity credentials (`backend/src/msai/services/live/broker_credentials_store.py:339`). API startup probes Key Vault secret **properties**, and fails closed if unavailable while active deployments exist (`backend/src/msai/main.py:279`). These are real production startup dependencies; the development path uses a local owner-only JSON store instead.

Compose interpolates service environments before profile exclusion. The development supervisor still has required `IB_ACCOUNT_ID`, `TWS_USERID`, and `TWS_PASSWORD` substitutions at `docker-compose.dev.yml:232`; production has analogous requirements. Thus the comment that non-broker startup needs no IB credentials is not a sufficient startup guarantee. This subtask did not render either real environment or retrieve any credential values.

## Startup side effects and current inspection boundary

The backend is not a purely passive database viewer:

- `backend/src/msai/main.py:305` bootstraps an API-key admin user if configured; `:138` starts projection consumers and loads active deployment stream identities from PostgreSQL.
- `main.py:307` starts the periodic TCP broker probe; `:311` starts the long-lived IB account snapshot. `backend/src/msai/services/ib_probe.py:43` opens the configured TCP endpoint; `ib_account_snapshot.py:353` calls `connectAsync`. Omitting the broker profile does **not** disable these API-owned connections.
- Development credentials-store construction creates/chmods its JSON store under `DATA_ROOT` (`backend/src/msai/services/live/broker_credentials_store.py:143`). Keeping the existing data directory is therefore an intentional existing-state inspection, not an immutable forensic mount.
- No production-code consumer of `IB_ALLOW_MOCK_FALLBACK` was found; its occurrence in dev Compose is not a broker-disable switch.
- `WorkerSettings` consumes backtests and ingestion, schedules PnL aggregation, nightly ingestion, and watchdog cleanup (`backend/src/msai/workers/settings.py:125`). The dev `job-watchdog` is another instance of this whole worker configuration (`docker-compose.dev.yml:196`), not a narrowly read-only watchdog. `DAILY_INGEST_ENABLED=false` stops the nightly wrapper (`backend/src/msai/workers/nightly_ingest.py:139`); it does not stop arbitrary already queued jobs.
- The live supervisor consumes/reclaims commands and rescans eligible deployments, as mapped in live-safety. Not starting it is the important protection against lifecycle recovery spawning an old deployment during app inspection. Merely omitting a new start request is insufficient if a supervisor is already running.

**Parent-observed local scope:** initially started `postgres`, `redis`, `backend`, and `frontend` using the root development Compose file plus an override. Existing local DB/data were preserved to assess current state. The override cleared vendor credentials, set `DAILY_INGEST_ENABLED=false`, `AUTO_HEAL_MAX_SYMBOLS=0`, set `IB_GATEWAY_HOST=127.0.0.1` and `IB_PORT=65534`, and cleared `GATEWAY_CONFIG`. After inspection, the parent also started backtest, research and portfolio workers to execute bounded real jobs; neither ingest-worker/job-watchdog nor broker/live-supervisor was started. Seven services became healthy; baseline live records were ten stopped and one failed. Consult [runtime assessment](runtime-assessment.md) and [actual API evidence](local-runtime-evidence.json) for completed jobs. The override is retained as [local-runtime.override.yml](local-runtime.override.yml). This is a guarded research session, not the unrestricted default development startup.

The closed port is in the backend container's own loopback namespace; it is not a host gateway. This prevents the unconditional API broker tasks from reaching a broker in the chosen topology. `IB_HOST` has precedence over `IB_GATEWAY_HOST` in settings (`backend/src/msai/core/config.py:91`), so an additional override source must not reintroduce a reachable `IB_HOST`. Empty vendor credentials and disabled workers reduce accidental external data activity; they are not substitutes for inspecting which containers/processes are already running.

Broker-dependent account summary/portfolio should return explicit unavailable state in this setup (`backend/src/msai/api/account.py:153`, `:192`), rather than simulated balances. Empty or failed broker cards are expected and should not be represented as a failed app startup or evidence of zero account exposure.

## Concise root startup sequence for this guarded local scope

This is a reproduction sequence for the parent's already authorized existing-data inspection, not a command to run against Azure. It assumes the reviewed temporary override remains available and the current local Compose interpolation prerequisites are satisfied. Do not print the fully resolved Compose environment or read a credential file to satisfy a missing value.

1. From `/Users/pablomarin/Code/msai-v2`, inventory current containers and confirm no already-running broker/supervisor/arq container is being mistaken for an excluded service. Record image IDs and port bindings. If another trading session is present, leave it alone; explicit service selection does not stop it.
2. Use the reviewed temporary override with broker target restricted to container loopback, vendor keys empty, scheduled ingest disabled, auto-heal symbol budget zero, and `GATEWAY_CONFIG` empty. Keep API/browser on matching local API-key settings. Where multiple environment sources exist, set both `IB_HOST` and `IB_GATEWAY_HOST` to loopback.
3. Start only the selected services, in dependency order. The following uses a variable pointing to that **existing reviewed override**, not a generated secret file:

```bash
cd /Users/pablomarin/Code/msai-v2
# Set MSAI_AUDIT_OVERRIDE to the reviewed temporary override path.
docker compose -f docker-compose.dev.yml -f "$MSAI_AUDIT_OVERRIDE" \
  up -d --wait postgres redis
docker compose -f docker-compose.dev.yml -f "$MSAI_AUDIT_OVERRIDE" \
  up -d --no-deps --wait backend
docker compose -f docker-compose.dev.yml -f "$MSAI_AUDIT_OVERRIDE" \
  up -d --no-deps --wait frontend
```

4. Verify local liveness at `http://127.0.0.1:8800/health` and browse `http://localhost:3300`. Use API-key mode and read/list operations. No worker, broker, supervisor, migration, image rebuild, or queue-drain command belongs in this inspection sequence. Missing images/dependencies/schema are distinct follow-up work, not reasons to broaden startup silently.

For a future clean-room preview, use separate database/data storage as well as a new Compose project, because the checked-in volume name and fixed dev container names defeat project-name-only isolation. No such second stack was created by this audit.

## Local authentication accepted by the app

`frontend/src/lib/auth.ts:20` explicitly documents three UI bypass modes: development, `NEXT_PUBLIC_E2E_AUTH_BYPASS=1`, or a configured `NEXT_PUBLIC_MSAI_API_KEY`. They bypass the login redirect, not backend authentication. `frontend/src/lib/api.ts:64` sends `X-API-Key` when no bearer token is present; `backend/src/msai/core/auth.py:106` validates it against the configured value. The root dev Compose supplies a matching local key by default.

Use this existing application mode for local inspection, consistent with `docs/agent-context.md:302`. No JWT forging, real Entra login, or secret extraction is needed. A browser UI bypass flag alone cannot authenticate backend calls. Production JWT scope/issuer/audience validation and production authorization remain separate findings in product-operations.

## Remote read-only evidence collection

Run only in an already authorized VM shell/Azure CLI session. These commands inspect metadata and unauthenticated liveness; they do not deploy, restart, log in to a broker, execute jobs, dump databases, or retrieve secrets. If access is unavailable, record the missing evidence rather than changing NSG/RBAC/identity configuration. Do not run `deploy-on-vm.sh`, `backup-to-blob.sh`, `gateway-watchdog.sh`, render-env services, `docker compose up`, or diagnostic CLI commands that mutate alerts/counters as inspection shortcuts.

### VM images, lifecycle, ports, and service health

```bash
# Current containers in the documented production Compose project.
sudo -n docker ps -a --filter label=com.docker.compose.project=msai \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

# Fields only: do not print Config.Env, full inspect JSON, or health log output.
for cid in $(sudo -n docker ps -aq --filter label=com.docker.compose.project=msai); do
  sudo -n docker inspect "$cid" --format \
    '{{.Name}} service={{index .Config.Labels "com.docker.compose.service"}} image={{.Config.Image}} image_id={{.Image}} state={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} started={{.State.StartedAt}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}}'
  sudo -n docker inspect "$cid" --format \
    '{{.Name}} {{range .Mounts}}mount={{.Type}}:{{.Name}}->{{.Destination}} {{end}}'
done

# Image content identities; compare with the intended CI artifact separately.
for iid in $(sudo -n docker ps -aq --filter label=com.docker.compose.project=msai \
  | xargs -r sudo -n docker inspect --format '{{.Image}}' | sort -u); do
  sudo -n docker image inspect "$iid" --format '{{.Id}} {{json .RepoDigests}}'
done

sudo -n docker volume ls --filter name=msai --format 'table {{.Name}}\t{{.Driver}}'
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/health
```

Container metadata and `/health` are not trading readiness. Gateway health is a TCP listener check, not fresh account reconciliation. Workers without configured health checks may show `not-configured`; that is not equivalent to either healthy or failed. The dev worker check is only a Redis ping (`docker-compose.dev.yml:50`), not proof that a job can complete.

Do **not** call `/ready` in a strictly read-only production pass: it issues `SELECT 1` and then retries API-key-user creation (`backend/src/msai/main.py:552`). The parent's local `/ready` call was within the intentionally mutable local app-startup scope. Do not use an authenticated `/system/health` or account refresh as a replacement without reviewing its external probe behavior.

### Host timers and backup job status

```bash
systemctl list-timers --all backup-to-blob.timer msai-gateway-watchdog.timer
systemctl show backup-to-blob.timer msai-gateway-watchdog.timer \
  -p Id -p ActiveState -p SubState -p LastTriggerUSec -p NextElapseUSecRealtime
systemctl show backup-to-blob.service msai-gateway-watchdog.service msai-render-env.service \
  -p Id -p ActiveState -p SubState -p Result -p ExecMainStatus \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp
```

The gateway-watchdog timer can recreate a gateway outside a manual Compose operation (`scripts/msai-gateway-watchdog.timer`, `scripts/gateway-watchdog.sh:133`). Its script calls the application to update counters/alerts; invoking it is not read-only. Routine deployment explicitly excludes broker-profile services, but installs/enables this host automation. Do not conclude the broker lifecycle is inactive merely because a deployment command omitted the broker profile.

### Azure metadata and backup blob inventory

Use actual values already established by the authorized resource inventory, not old hardcoded runbook examples. The checked-in Bicep exposes non-secret backup account/container outputs at `infra/main.bicep:793`. These commands rely on the existing CLI session and explicitly use login-based blob reads; they do not request storage keys or Key Vault secrets.

```bash
az account show --query '{subscription:name,id:id,tenant:tenantId}' -o json
az vm get-instance-view -g "$MSAI_RG" -n "$MSAI_VM" \
  --query '{name:name,location:location,statuses:instanceView.statuses}' -o json
az deployment group list -g "$MSAI_RG" \
  --query '[].{name:name,state:properties.provisioningState,time:properties.timestamp}' -o table
az deployment group show -g "$MSAI_RG" -n "$MSAI_DEPLOYMENT" \
  --query '{storage:properties.outputs.backupsStorageAccount.value,container:properties.outputs.backupsContainerName.value}' -o json

# Set MSAI_BACKUP_STORAGE and MSAI_BACKUP_CONTAINER from those metadata outputs.
az storage blob list --auth-mode login \
  --account-name "$MSAI_BACKUP_STORAGE" --container-name "$MSAI_BACKUP_CONTAINER" \
  --prefix backup- \
  --query "sort_by([?ends_with(name, '/postgres.sql.gz')], &properties.lastModified)[-10:].{name:name,bytes:properties.contentLength,modified:properties.lastModified}" \
  -o table
```

Inspect Parquet object metadata for the same selected backup prefix if needed; do not download database contents during this pass. Timer success, blob size, and object freshness establish recent backup artifacts, not restorability. The source script performs a timestamped PostgreSQL dump and optional Parquet copy (`scripts/backup-to-blob.sh:73`, `:113`), skips missing/empty Parquet, and does not back up Redis state. A restore test is separate authorized work, despite the repository already containing a restore runbook.

## Evidence and remaining limits

Source inspection establishes startup dependencies and configuration differences. The parent-observed local HTTP success establishes that the guarded app starts against the current local state; it does not prove research jobs, live execution, production SSO, broker connectivity, Azure service health, backup freshness, or recoverability. No claim about the currently deployed image SHA, runtime port binding, timer state, or backup age should be made until the corresponding runtime metadata is collected.

Recommended next evidence is narrow: finish local inventory/counts and browser/API read paths; collect VM image/digest/health/timer metadata and backup object timestamps; then reconcile those observations with this source map. Any future broker acceptance must use a freshly verified, explicitly authorized test account.
