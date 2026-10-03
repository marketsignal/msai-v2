# Azure and GitHub runtime inventory

Observed 2026-10-03, 17:33–17:37 UTC. Read-only checks of Azure resource metadata, GitHub workflow metadata/filtered errors, and public HTTP endpoints. No Azure changes, deployments, broker startup, remote command execution, secret retrieval, or backup downloads occurred in this assessment. A public SSH known-host key was copied from a GitHub repository variable to a temporary file for the parent assessor's separately owned strict-host-verification inspection.

## Current observed state

Production is reachable, but delivery of the last two commits is blocked by a stale temporary SSH rule. The latest successful deployment targeted `71aa4a9`; the local checkout and remote main are at `7f9eb2b`. Those two intervening commits change the Forge harness, not the inspected application, lockfiles, deployment scripts, or infrastructure. A subsequent parent-agent SSH inspection confirmed API/frontend/research-worker images tagged `71aa4a9`, but the supervisor is still tagged `65ae682`. This is a mixed runtime; workflow success alone did not reveal the older broker-profile component. Parent-agent results are distinguished below from this agent's metadata/public-HTTP checks.

| Check | Observed result | What it establishes |
| --- | --- | --- |
| `GET https://platform.marketsignal.ai/health` | HTTP 200, `{"status":"healthy","environment":"production"}` | API process responds publicly as production. |
| `GET https://platform.marketsignal.ai/ready` | HTTP 200, `{"status":"ready"}` | The implemented database-readiness probe succeeds; it does not prove all workers or broker health. |
| `GET https://platform.marketsignal.ai/` | HTTP 200 | Public frontend responds. No browser login or interactive journey was exercised here. |
| `GET https://platform.marketsignal.ai/api/v1/system/health`, without credentials | HTTP 401, `Missing Authorization header or X-API-Key` | This protected API rejects an anonymous request. It does not validate Entra login, token refresh, or role authorization. |
| Existing GitHub authentication | Authenticated request returned the expected `pablomarin` identity | Existing GitHub credentials are usable; no token was displayed. |
| Existing Azure authentication | Resource reads succeed in the configured MarketSignal2 subscription | Existing Azure credentials support control-plane reads. Backup data-plane access is narrower. |

The Azure CLI initially selected `Microsoft Azure Sponsorship`, where `msaiv2_rg` does not exist. GitHub's deployment variable identifies subscription `68067b9b-943f-4461-8cb5-2bc97cbc462d`, named **MarketSignal2**, and resource group `msaiv2_rg`. Subsequent commands explicitly supplied `--subscription`; the default account selection was not changed. The initial `ResourceGroupNotFound` result was wrong-subscription context, not a missing production deployment.

## Azure resources

The configured resource group contains 22 resources. Its VM is `msai-vm`, **running**, provisioning state **Succeeded**, **Standard_D4ds_v6**, Linux, region **eastus2**. The provisioning image is Canonical `ubuntu-24_04-lts` / `server`, exact image version `24.04.202605060`. This image metadata is not the currently patched OS package/kernel version.

The attached `msai-data-disk` is **128 GB Premium_LRS** (`137438953472` bytes). The OS disk is also attached Premium_LRS; its capacity was not established by this bounded query. Network metadata includes the expected VNet, NIC, public IP, and NSG. The public VM address obtained from the existing deployment variable is `40.75.8.153`.

| Resource category | Observed resources |
| --- | --- |
| Execution / network | `msai-vm`, `msai-data-disk`, OS managed disk, `msai-vnet`, `msai-nic`, `msai-pip`, `msai-nsg` |
| Identity / secrets | `msai-gh-oidc` managed identity; `msai-kv-4cd6d2obcxqaa` Key Vault. Metadata only; no secrets read. |
| Images / backups | `msaiacr4cd6d2obcxqaa` ACR; `msaibk4cd6d2obcxqaa` Storage account |
| Monitoring | Log Analytics workspace, AzureMonitorLinuxAgent VM extension, `msai-heartbeat-dcr`, `msai-app-insights`, `msai-health-ping`, operator action group and Smart Detection action group |
| Alert definitions | Backup-failure, health-availability, orphan-NSG, and container-restart-heuristic scheduled query rules |

Resource presence does not establish that telemetry is currently being ingested or alerts delivered. No container listing, worker probe, broker-account identity, active-position inspection, or monitoring-data query was performed by this inventory agent.

### Parent-agent read-only VM observations

The parent assessor subsequently supplied these results from its separately owned, strictly verified SSH inspection. They were not independently repeated by this agent:

| VM observation | Result and limit |
| --- | --- |
| Container image tags | API, frontend, and research workers: `71aa4a9`; live supervisor: `65ae682`, up approximately three months; IB Gateway: `10.43.1c`, up approximately 18 hours, healthy. Tags are identified; immutable digests were not supplied in this message. |
| Protected API/system health | HTTP 200; reported subsystems healthy, but version and commit fields are `unknown`. This matches the source's fallback behavior and shows that endpoint metadata alone cannot identify the release. |
| Current inventory via API | Five strategies; 11 backtest-history entries, all complete; zero research jobs; three portfolios; AAPL/SPY, 33 data files totaling about 6.56 MB; four live deployments, all stopped. These are observed current records, not comprehensive historical trading proof. |
| Broker-account registry | One active row labeled “LVP prod low-value test (real-money drill).” This differs from documentation naming HVP for production. A label is not the actual connected IB identity; the parent owns that identity investigation. |
| Host timers | Backup timer last completed 2026-10-03 02:11:21 UTC with status 0; gateway watchdog last completed 17:36:52 UTC with status 0. Backup success is host execution evidence, not a restore test or independent Blob listing. |
| Filesystem verification | Parent confirmed `findmnt -T /var/lib/msai` resolves to root `/dev/nvme0n2p1`; Docker application/database volumes are below `/var/lib/msai/docker/volumes`. Root is 83% used, about 11 GB available. The attached 128 GB device has no mounted filesystem; the intended application data disk is not in use. |

### Supervisor revision difference

`git diff 65ae682..71aa4a9` shows supervisor-relevant changes in `backend/src/msai/live_supervisor/__main__.py` and `backend/src/msai/services/nautilus/strategy_loader.py`, plus the new `backend/src/msai/services/strategy_paths.py` resolver. The old loader used `Path(strategy_file).resolve()`, resolving relative paths against process working directory. The new loader resolves against configured `strategies_root` (`backend/src/msai/services/nautilus/strategy_loader.py:36,90`), and the supervisor uses that same resolver for code hashes (`backend/src/msai/live_supervisor/__main__.py:372-382`). The intervening `71aa4a9` change also canonicalizes registry file paths.

This creates a concrete compatibility concern: an old supervisor receiving a new relative registry path may look under `/app/<path>` instead of `/app/strategies/<path>`. That next-spawn failure was not executed here. The parent was asked to inspect actual registry paths and old-container file existence without starting a deployment. There are no diffs across these tags in `backend/uv.lock`, `backend/pyproject.toml`, `strategies`, `services/live`, `trading_node_subprocess.py`, or `live_node_config.py`; avoid claiming the entire live-safety implementation is a different version merely from tag age.

## GitHub release evidence

Local checkout and GitHub main both resolve to `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`.

| Workflow | Revision | Started / ended UTC | Outcome |
| --- | --- | --- | --- |
| [Current CI](https://github.com/marketsignal/msai-v2/actions/runs/37114510519) | `7f9eb2b` | 09:52:30 / 10:09:12 | Success. Frontend job completed 09:53:46; image-data-path 09:53:33; backend 10:09:12. |
| [Current auth gate](https://github.com/marketsignal/msai-v2/actions/runs/37114510512) | `7f9eb2b` | 09:52:30 / 09:53:03 | Success. |
| [Current image build](https://github.com/marketsignal/msai-v2/actions/runs/37114510578) | `7f9eb2b` | 09:52:30 / 09:53:11 | Success. |
| [Latest deployment attempt](https://github.com/marketsignal/msai-v2/actions/runs/37114548488) | `7f9eb2b` | 09:53:13 / 09:54:33 | Failure at “Open transient SSH allow rule.” VM staging and deployment execution were skipped. |
| [Previous deployment attempt](https://github.com/marketsignal/msai-v2/actions/runs/37093997222) | `c244426` | 03:40:36 / 03:41:12 | Same failed step; VM staging and deployment execution skipped. |
| [Latest successful deployment](https://github.com/marketsignal/msai-v2/actions/runs/37092738955) | `71aa4a9` | 03:18:07 / 03:22:03 | Deploy and cleanup jobs both reported success. |

The full current CI result materially strengthens verification beyond this audit's limited local checks. It does not prove today’s interactive frontend or production broker paths; see the workflow source for which checks run. The inventory did not rerun any workflow.

**The CI/deploy sequencing gap is now observed, not merely inferred from YAML:** the latest deploy job began at 09:53:16 and failed at 09:53:35, while the backend CI job continued until 10:09:12. Deployment was eligible before full CI finished. The failed SSH rule prevented this attempt from reaching the VM.

Main-branch protection is enabled, but its required check names are only:

- `Gate 1 — MSAL scope lint`
- `Gates 2+3 — JWT contract + identity-contract lint`

The protection endpoint reports `enforce_admins: false` and `required_approving_review_count: 0`. The backend/frontend/image-data-path CI jobs are absent from the reported required status checks. This confirms that branch protection, as inspected, does not close the CI/deploy gap identified in `product-operations.md` PO-04.

## Current operational fault: stale SSH rule plus disabled reaper

The latest deployment error is:

```text
SecurityRuleConflict: Security rule gha-transient-37092738955-1 conflicts
with rule gha-transient-37114548488-1.
Rules cannot have the same Priority and Direction.
```

Current Azure NSG metadata confirms `gha-transient-37092738955-1` remains present as an inbound Allow rule for port 22 at priority 200. The deploy workflow always creates its new transient rule at priority 200 (`.github/workflows/deploy.yml:494-507`). Therefore the current failure is explained by an actual conflicting resource, not a speculative GitHub/Azure authentication problem.

The successful deployment's cleanup job reported success, but cleanup suppresses deletion failures with `2>/dev/null || true` (`.github/workflows/deploy.yml:666-679`). This proves that a green cleanup status is not deletion evidence. The cause of that older deletion failure is unknown because the command discards its diagnostics; this assessment did not invent a cause.

The fallback [NSG reaper workflow](https://github.com/marketsignal/msai-v2/blob/main/.github/workflows/reap-orphan-nsg-rules.yml) is currently **`disabled_inactivity`**, updated at 2026-08-10 04:57 UTC. Its latest observed [run](https://github.com/marketsignal/msai-v2/actions/runs/31357080594) started 2026-08-10 04:57 UTC and succeeded. Although its source config schedules it every 15 minutes (`.github/workflows/reap-orphan-nsg-rules.yml:9-13`), that cleanup protection is not currently active.

Another inbound port-22 rule named `claude-hvp-session-ssh` exists at priority 250, in addition to `AllowSshFromOperator` at priority 100. Source-address restrictions were not inspected, so this is an item for scoped review, not a claim that SSH is open to the entire internet. No NSG rule was added, edited, or deleted.

**Next operational action, requiring the separate authorized repair scope:** verify the orphan's ownership/inactivity, remove the specific stale deployment rule, restore a reliable reaper or equivalent cleanup, and make failed deletion visible. Then retry an explicitly authorized deployment and verify the candidate actually reaches the VM. None of those mutations was performed here.

## Local versus Azure revision comparison

| Layer | Local | Azure / deployment evidence | Interpretation |
| --- | --- | --- | --- |
| Git revision | `7f9eb2b5ad252bc3ba730f53a159ca04046278eb` | Latest successful deployment targeted `71aa4a9a47a3b96e2e3c90c0c00c42d3e47f8105`; parent confirmed API/frontend/research-worker tags `71aa4a9`, supervisor tag `65ae682` | Main application is two Forge-only commits behind; supervisor is older and lacks the canonical strategy-path resolver. |
| Intervening commits | `c244426`: Forge 6.4.0; `7f9eb2b`: Forge 6.4.1 | Not delivered by the two failed deployment attempts | Harness change, not a demonstrated application-version mismatch. |
| Application / delivery paths | No changes from `71aa4a9` to HEAD in `backend`, `frontend`, `strategies`, `docker-compose.prod.yml`, `infra`, `scripts`, or `.github` | Same tracked source bytes for those inspected paths at the last successful deployment revision | Does not prove matching environment, volumes, manually edited VM files, image digest, or installed base-image packages. |
| Backend dependency lock | Git blob `1907c4602150b6f02a0eeef7202d295d537ed8f6` | Same blob at `71aa4a9` | No locked dependency difference across these revisions. |
| Frontend dependency lock | Git blob `da59857168566199bea9450ffda597b3cd84ec64` | Same blob at `71aa4a9` | No locked dependency difference across these revisions. |
| Local execution | Parent assessor owns local startup and runtime checks | Public production endpoints respond | Local and Azure runtime equivalence was not inferred from source equality. |

The public probes do not return an application commit identifier. The protected system-health endpoint has a commit field, but it resolves from `GITHUB_SHA` or `git rev-parse`, falling back to `unknown` (`backend/src/msai/api/system.py:68-85`). It was not queried with credentials in this inventory. Use actual container metadata and immutable digest evidence in the parent runtime report for stronger identification.

## Backup evidence limitation

A metadata listing from the local Azure identity was denied for insufficient Storage Blob data permissions. The coordinator then used the VM's existing managed-identity session with `--auth-mode login`, without keys or backup downloads. Today's `backup-20261003T021117Z` exists: PostgreSQL gzip is **2,751,925 bytes**, modified **02:11:19 UTC**, and **33 Parquet blobs** were modified at **02:11:20 UTC**. The host timer exited successfully at 02:11:21 UTC. Current backup existence/freshness is established; contents and restorability remain **unverified**.

## Reproducible read-only commands

These commands produced the inventory, workflow status, and public probe observations. Queries intentionally avoid secret values and VM custom data. GitHub workflow error logs were filtered to `ERROR:`, `Code:`, `Message:`, and `##[error]` lines before display; full logs were not saved.

```sh
az account show --query '{name:name,state:state,isDefault:isDefault,userType:user.type}' -o json
gh api user --jq .login
gh variable get AZURE_SUBSCRIPTION_ID --repo marketsignal/msai-v2
gh variable get RESOURCE_GROUP --repo marketsignal/msai-v2
az resource list --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d -g msaiv2_rg \
  --query '[].{name:name,type:type,location:location}' -o json
az vm list --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d -g msaiv2_rg -d \
  --query '[].{name:name,powerState:powerState,size:hardwareProfile.vmSize,location:location,os:storageProfile.osDisk.osType}' -o json
az network nsg rule list --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d \
  -g msaiv2_rg --nsg-name msai-nsg \
  --query '[].{name:name,priority:priority,direction:direction,access:access,destinationPort:destinationPortRange}' -o json
gh run list --repo marketsignal/msai-v2 --workflow deploy.yml --limit 8 \
  --json databaseId,status,conclusion,headSha,createdAt,updatedAt,url
gh run view 37114548488 --repo marketsignal/msai-v2 --json jobs
gh run view 37114510519 --repo marketsignal/msai-v2 --json jobs
gh api repos/marketsignal/msai-v2/actions/workflows/reap-orphan-nsg-rules.yml \
  --jq '{name,state,updated_at,html_url}'
gh api repos/marketsignal/msai-v2/branches/main/protection \
  --jq '{required_status_checks:.required_status_checks.contexts,enforce_admins:.enforce_admins.enabled,required_approvals:.required_pull_request_reviews.required_approving_review_count}'
curl --silent --show-error --max-time 20 --write-out '\nHTTP %{http_code}\n' \
  https://platform.marketsignal.ai/health
curl --silent --show-error --max-time 20 --write-out '\nHTTP %{http_code}\n' \
  https://platform.marketsignal.ai/ready
curl --silent --show-error --max-time 20 --output /dev/null \
  --write-out 'frontend HTTP %{http_code}\n' https://platform.marketsignal.ai/
curl --silent --show-error --max-time 20 --write-out '\nHTTP %{http_code}\n' \
  https://platform.marketsignal.ai/api/v1/system/health
git diff --name-only 71aa4a9a47a3b96e2e3c90c0c00c42d3e47f8105..HEAD \
  -- backend frontend strategies docker-compose.prod.yml infra scripts .github
git rev-parse 71aa4a9:backend/uv.lock HEAD:backend/uv.lock \
  71aa4a9:frontend/pnpm-lock.yaml HEAD:frontend/pnpm-lock.yaml
```

This inventory establishes current public availability, a concrete release-pipeline fault, deployed image versions and fresh backup metadata. The coordinator additionally reproduced the old supervisor's `FileNotFoundError` for the currently stored `example/ema_cross.py` path, without a live start. See [runtime assessment](runtime-assessment.md) for owner SSO, real local workflows and account-registry/snapshot disagreement. Trading readiness, portfolio correctness, partner authorization, broker-wide flatness and restore remain unverified.
