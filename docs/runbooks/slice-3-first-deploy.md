# Runbook: First Real Production Deploy (Slice 3 acceptance)

**Purpose:** Run the first real production deploy after Slice 3 merges. The result is captured as Slice 3 acceptance evidence in `docs/CHANGELOG.md`.

**Pre-conditions (must be true before starting):**

- Slice 3 PR merged to `main`
- Hawk's gate honored: `scripts/backup-to-blob.sh` ran against the empty prod Postgres + dump verified in `msai-backups` Blob (PR description has evidence)
- Contrarian's gate honored: `docs/runbooks/slice-3-rehearsal.md` smoked clean + RG torn down (PR description has evidence)
- Operator pre-flight checklist (PRD §10) all checked off

---

## 0. Verify pre-flight

```bash
# DNS
dig +short platform.marketsignal.ai
# expected: <prod VM public IP from Slice 1 output>

# Backend Entra app reg has the redirect URI
# (manual step in Azure Portal: Backend's app reg → Authentication → check
#  https://platform.marketsignal.ai/auth/callback is listed under SPA platform)

# Repo Variables (sample of the new Slice 3 ones)
gh variable list | grep -E "MSAI_HOSTNAME|VM_PUBLIC_IP|VM_SSH_USER|VM_SSH_KNOWN_HOSTS|NSG_NAME|KV_NAME|DEPLOYMENT_NAME|MSAI_BACKEND_IMAGE|MSAI_FRONTEND_IMAGE"
# expected: all 9 listed
```

## 1. Trigger first deploy via workflow_dispatch (NOT auto)

The `workflow_run` trigger is sometimes silently skipped on the very first invocation after a workflow file is added (research §5 finding 6). Trigger the first deploy manually for the merge commit:

```bash
MERGE_SHA=$(git rev-parse --short=7 main)
gh workflow run deploy.yml -f git_sha="$MERGE_SHA"
gh run watch
```

The deploy job will:

1. Open `gha-transient-${run_id}-1` SSH rule for the runner's IP
2. SSH to the prod VM, execute `deploy-on-vm.sh`
3. Run runner-side acceptance probes (TLS chain, /health, frontend root)
4. Cleanup job deletes the transient rule

If the run fails, check `gh run view --log <run-id>` for the FAIL\_<X> marker on the last line of stderr. Diagnostic table:

| Marker                 | Likely cause                                        | Fix                                                                                                    |
| ---------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `FAIL_ENV`             | Missing repo Variable; bad git_sha format           | Set Variable; pass valid 7-hex SHA                                                                     |
| `FAIL_AZ_LOGIN`        | VM doesn't have system-assigned MI                  | Confirm Slice 1 deploy applied; `az vm identity show -g <rg> -n msai-vm`                               |
| `FAIL_ACR_LOGIN`       | AcrPull RBAC not yet propagated                     | Wait 60s, retry. Verify with `az role assignment list --assignee <vm-mi-objectId>`                     |
| `FAIL_RENDER_ENV`      | KV not seeded; KV_NAME wrong; MI lacks Secrets User | `az keyvault secret list --vault-name <kv>`                                                            |
| `FAIL_CADDY_VALIDATE`  | Caddyfile typo committed                            | Fix locally, push, re-run build-and-push.yml + deploy.yml                                              |
| `FAIL_PULL`            | ACR image not present for the SHA                   | Verify `az acr repository show-tags --name <acr> --repository msai-backend` includes the SHA           |
| `FAIL_MIGRATE`         | Alembic migration error in this image               | Roll back to last-good SHA: `gh workflow run deploy.yml -f git_sha=<previous>`                         |
| `FAIL_PROBE_HEALTH`    | Backend startup failure                             | `ssh ${VM_SSH_USER}@${VM_PUBLIC_IP} 'sudo docker compose --project-name msai logs backend --tail=200'` |
| `FAIL_PROBE_TLS`       | DNS, NSG 443, or LE issuance issue                  | `dig`, `nc -zv vm 443`, `docker compose logs caddy`                                                    |
| `FAIL_ROLLBACK_OK`     | Deploy failed but rollback restored last-good SHA   | Investigate failure cause; user-facing impact: brief outage during rollback                            |
| `FAIL_ROLLBACK_BROKEN` | Deploy failed AND rollback failed                   | **Page yourself.** SSH manually, restore from Hawk's-gate backup if needed                             |

## 2. Verify 5/5 acceptance probes

Same probes as `slice-3-rehearsal.md` step 8, against `platform.marketsignal.ai`:

```bash
HOST=platform.marketsignal.ai

curl -sf https://$HOST/health && echo "✓ /health"
curl -sf https://$HOST/ready && echo "✓ /ready"
[[ "$(curl -sI -o /dev/null -w '%{http_code}' https://$HOST/)" == "200" ]] && echo "✓ frontend"
echo | openssl s_client -connect $HOST:443 -servername $HOST 2>/dev/null \
    | openssl x509 -noout -issuer | grep -qi "Let's Encrypt" && echo "✓ LE cert"
[[ "$(curl -sI -o /dev/null -w '%{http_code}' https://$HOST/api/v1/auth/me)" == "401" ]] && echo "✓ api proxied"
```

5/5 must pass.

## 3. Capture acceptance evidence

Append a new Slice 3 entry to `docs/CHANGELOG.md`:

```markdown
## 2026-05-XX — Slice 3 of 4: SSH Deploy + First Real Production Deploy

PR #XX merged. First real production deploy: `<sha7>` deployed to `platform.marketsignal.ai`. Acceptance smoke 5/5 PASS:

- /health 200
- /ready 200
- Frontend root 200
- LE cert chain valid
- /api/v1/auth/me 401 (Caddy prefix-preserving proxy confirmed)

Deploy run: <gh run URL>
Hawk's gate evidence: <PR description link>
Contrarian's gate evidence: <PR description link>

Slice 4 carry-over: nightly backup cron via azcopy, Log Analytics dashboards/alerts, active-`live_deployments` hard refusal gate.
```

## 4. Post-deploy hygiene

- **Verify reaper is firing:** `gh run list --workflow=reap-orphan-nsg-rules.yml --limit 3` — at least one fire in the last hour. Open run summary; reaped count should be 0 (no orphans).
- **Confirm no leaked NSG rule from the deploy run:** `az network nsg rule list -g $RG --nsg-name $NSG_NAME --query "[?starts_with(name, 'gha-transient-')]"` — empty.
- **Cycle a rollback** as a smoke: `gh workflow run deploy.yml -f git_sha=<sha7>` (same SHA — idempotent re-deploy proves the rollback path works on prod). If the re-run is green, you have empirical proof the rollback machinery works on prod.

## Orphan rule diagnosis (if needed)

See `docs/decisions/deploy-ssh-jit.md` "Operator runbook — orphan rule diagnosis".
