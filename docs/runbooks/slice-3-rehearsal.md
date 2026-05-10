# Runbook: Slice 3 Rehearsal in Throwaway Resource Group

**Purpose:** Honor the Contrarian's blocking gate (council slicing verdict §Slice 3, Blocking Objection #4) — the full VM deploy path MUST be rehearsed end-to-end in a throwaway resource group before merge. This runbook is the procedure.

**When:** Run AFTER Slice 3 implementation is complete, BEFORE merging the PR.
**Estimated time:** ~90 min wall clock (~30 min hands-on).
**Cost:** A throwaway D4s_v6 VM + ACR + KV + storage for ~90 min ≈ $1.

---

## Pre-flight

1. **Confirm LE rate-limit headroom** for `marketsignal.ai` registered domain. LE limit: 50 certs / week / registered domain. Check via `crt.sh`:

   ```bash
   curl -s "https://crt.sh/?q=marketsignal.ai&output=json" | jq '[.[] | select(.entry_timestamp > "'"$(date -u -d '7 days ago' '+%Y-%m-%dT%H:%M:%S')"'")] | length'
   ```

   If ≥ 40 issuances in last 7 days, postpone rehearsal — bursting now risks the production deploy hitting the rate limit.

2. **Confirm rehearsal hostname has an A record:** `platform-rehearsal.marketsignal.ai` → reachable IP. Will update to throwaway VM's IP in step 4.

3. **Confirm operator IP** is current (Slice 1 NSG only allows SSH from `operatorIp/32`).

4. **Set rehearsal-only Variables on the GH repo OR override per-step:** `MSAI_HOSTNAME=platform-rehearsal.marketsignal.ai`, `RESOURCE_GROUP=msaiv2-rehearsal-<YYYYMMDD>`, `NSG_NAME` will be the rehearsal NSG name (`msai-nsg` per Slice 1 default — same name in different RG is fine), `VM_PUBLIC_IP` will be the rehearsal VM's IP, `VM_SSH_KNOWN_HOSTS` will be the rehearsal VM's host key.

---

## 1. Provision throwaway RG (~10 min)

```bash
RG=msaiv2-rehearsal-$(date -u +%Y%m%d)
az group create --name "$RG" --location eastus2 --tags rehearsal=true expires-by="$(date -u -d '+1 day' +%Y-%m-%d)"
```

## 2. Apply Slice 1 IaC (~15 min, includes VM provisioning)

Generate a rehearsal-only SSH keypair (DO NOT reuse the prod key):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/msai-rehearsal -N '' -C "msai-rehearsal-$(date -u +%Y%m%d)"
```

Apply Bicep against the rehearsal RG. Use the placeholder bicepparam + CLI overrides:

```bash
OPERATOR_IP=$(curl -sf https://ifconfig.me)
OPERATOR_OID=$(az ad signed-in-user show --query id -o tsv)
SSH_PUB=$(cat ~/.ssh/msai-rehearsal.pub)

az deployment group create \
    --name msai-iac \
    --resource-group "$RG" \
    --template-file infra/main.bicep \
    --parameters infra/main.bicepparam \
    --parameters operatorIp="$OPERATOR_IP" operatorPrincipalId="$OPERATOR_OID" \
                 vmSshPublicKey="$SSH_PUB"
```

Capture outputs:

```bash
OUTS=$(az deployment group show --name msai-iac --resource-group "$RG" --query 'properties.outputs' -o json)
VM_IP=$(jq -r .vmPublicIp.value <<<"$OUTS")
ACR_NAME=$(jq -r '.acrLoginServer.value | split(".")[0]' <<<"$OUTS")
ACR_LOGIN=$(jq -r .acrLoginServer.value <<<"$OUTS")
KV_NAME=$(jq -r .keyVaultName.value <<<"$OUTS")
NSG_NAME=$(jq -r .nsgName.value <<<"$OUTS")
echo "VM_IP=$VM_IP ACR=$ACR_NAME KV=$KV_NAME NSG=$NSG_NAME"
```

## 3. Update DNS A record + capture host key

Point `platform-rehearsal.marketsignal.ai` → `$VM_IP` (TTL ≤ 300s). Wait ~60s for propagation.

Capture the rehearsal VM's host key:

```bash
ssh-keyscan -t ed25519 "$VM_IP" 2>/dev/null > /tmp/rehearsal-known-hosts
cat /tmp/rehearsal-known-hosts  # paste into VM_SSH_KNOWN_HOSTS Variable for the rehearsal run
```

## 4. Seed KV with rehearsal secrets (~5 min)

```bash
# Generate a rehearsal REPORT_SIGNING_SECRET
openssl rand -base64 48 | tr -d '\n' > /tmp/report-signing-secret

az keyvault secret set --vault-name "$KV_NAME" --name report-signing-secret --file /tmp/report-signing-secret --output none
az keyvault secret set --vault-name "$KV_NAME" --name postgres-password --value "rehearsal-pg-$(openssl rand -hex 8)" --output none
az keyvault secret set --vault-name "$KV_NAME" --name azure-tenant-id --value "${{ vars.AZURE_TENANT_ID }}" --output none
az keyvault secret set --vault-name "$KV_NAME" --name azure-client-id --value "${{ vars.AZURE_CLIENT_ID }}" --output none
az keyvault secret set --vault-name "$KV_NAME" --name cors-origins --value '["https://platform-rehearsal.marketsignal.ai"]' --output none
az keyvault secret set --vault-name "$KV_NAME" --name ib-account-id --value "DU<rehearsal-paper-account>" --output none
az keyvault secret set --vault-name "$KV_NAME" --name tws-userid --value "rehearsal-no-broker" --output none
az keyvault secret set --vault-name "$KV_NAME" --name tws-password --value "rehearsal-no-broker" --output none

rm /tmp/report-signing-secret
```

## 5. Trigger Slice 2 build for rehearsal SHA

```bash
gh workflow run build-and-push.yml
gh run watch
```

Note the resulting `<sha7>` — `git rev-parse --short=7 HEAD` gives the local copy.

## 6. **Contrarian's spike** (T13 child-resource refactor proof)

Before triggering deploy, prove the NSG child-resource refactor survives a Bicep reapply:

```bash
# Manually create a transient rule
az network nsg rule create -g "$RG" --nsg-name "$NSG_NAME" \
    --name gha-transient-spike-1 --priority 999 \
    --direction Inbound --access Allow --protocol Tcp \
    --source-address-prefixes "$OPERATOR_IP/32" --destination-port-ranges 22 \
    --output none

# Confirm the rule exists
az network nsg rule list -g "$RG" --nsg-name "$NSG_NAME" --query "[?name=='gha-transient-spike-1'] | length(@)"
# Expect: 1

# Reapply Bicep against the same RG
az deployment group create --name msai-iac --resource-group "$RG" \
    --template-file infra/main.bicep --parameters infra/main.bicepparam \
    --parameters operatorIp="$OPERATOR_IP" operatorPrincipalId="$OPERATOR_OID" \
                 vmSshPublicKey="$SSH_PUB"

# Confirm the transient rule SURVIVED
az network nsg rule list -g "$RG" --nsg-name "$NSG_NAME" --query "[?name=='gha-transient-spike-1'] | length(@)"
# MUST be: 1
```

If the rule was deleted, **STOP** — the child-resource refactor is broken; escalate before continuing.

Clean up the spike rule:

```bash
az network nsg rule delete -g "$RG" --nsg-name "$NSG_NAME" --name gha-transient-spike-1 --output none
```

## 7. Trigger Slice 3 deploy against rehearsal RG

```bash
gh workflow run deploy.yml -f git_sha="<sha7-from-step-5>" -f resource_group="$RG"
gh run watch
```

The deploy job:

1. Opens `gha-transient-${run_id}-1` rule for runner IP
2. SSHes, runs `deploy-on-vm.sh`
3. Cleanup job deletes the rule

## 8. Verify acceptance probes (5/5)

From the operator workstation:

```bash
# 1. /health 200
curl -sf https://platform-rehearsal.marketsignal.ai/health && echo "✓ health"

# 2. /ready 200
curl -sf https://platform-rehearsal.marketsignal.ai/ready && echo "✓ ready"

# 3. Frontend root
[[ "$(curl -sI -o /dev/null -w '%{http_code}' https://platform-rehearsal.marketsignal.ai/)" == "200" ]] && echo "✓ frontend"

# 4. TLS chain
echo | openssl s_client -connect platform-rehearsal.marketsignal.ai:443 \
    -servername platform-rehearsal.marketsignal.ai 2>/dev/null \
    | openssl x509 -noout -issuer | grep -qi "Let's Encrypt" && echo "✓ LE cert"

# 5. /api/v1/auth/me returns 401 (NOT 404) — proves prefix-preserving Caddy reverse-proxy
[[ "$(curl -sI -o /dev/null -w '%{http_code}' https://platform-rehearsal.marketsignal.ai/api/v1/auth/me)" == "401" ]] && echo "✓ api proxied"
```

All 5 must pass. If any fails, capture the failure and fix BEFORE merge.

## 9. Tear down (~2 min)

```bash
az group delete --name "$RG" --yes --no-wait
echo "Rehearsal RG deletion initiated: $RG"
```

## Evidence to capture in PR description

- `gh run` URL of the Slice 3 deploy against the rehearsal RG (success)
- Screenshot or paste of the 5/5 probe results
- `az group delete` confirmation (or `az group show -g "$RG"` returning ResourceGroupNotFound after a few minutes)
- Confirmation that the spike (step 6) showed the transient rule SURVIVED a Bicep reapply
