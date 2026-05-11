#!/usr/bin/env bash
# CI test for infra/main.bicep.
#
# Two checks:
#   1. `az bicep build` — lint clean, no warnings, valid syntax.
#   2. `az deployment group what-if` — no Delete operations on the target RG (only fail on
#      Delete; Create on first run is expected, Modify on subsequent runs is normal because
#      Azure adds default values not declared in Bicep).
#
# Skip what-if when SKIP_WHATIF=1 OR no Azure auth (CI without secrets).
#
# Override the dummy what-if params via TEST_OPERATOR_IP / TEST_OPERATOR_PRINCIPAL_ID /
# TEST_SSH_PUBKEY env vars. Defaults are valid placeholder values that pass type
# validation but produce a non-functional deployment if accidentally applied.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== az bicep build infra/main.bicep ==="
az bicep build --file infra/main.bicep --stdout >/dev/null
echo "Lint clean."

echo "=== Slice 2 grep assertions ==="

# AcrPush role-def variable (Slice 2)
grep -q "var roleDefIdAcrPush = subscriptionResourceId" infra/main.bicep \
    || { echo "FAIL: roleDefIdAcrPush variable missing in infra/main.bicep" >&2; exit 1; }

# AcrPush role assignment for the gh-oidc MI (Slice 2)
grep -q "resource ghOidcAcrPushAssignment 'Microsoft.Authorization/roleAssignments" infra/main.bicep \
    || { echo "FAIL: ghOidcAcrPushAssignment resource missing in infra/main.bicep" >&2; exit 1; }
grep -q "roleDefinitionId: roleDefIdAcrPush" infra/main.bicep \
    || { echo "FAIL: AcrPush role-def reference missing in role-assignment block" >&2; exit 1; }

echo "=== Slice 3 grep assertions ==="

# Slice 3 T13: NSG securityRules refactored to child resources (NOT inline property).
# If `securityRules:` appears as a property of the NSG, the refactor regressed.
grep -q "resource nsgRuleSshFromOperator 'Microsoft.Network/networkSecurityGroups/securityRules" infra/main.bicep \
    || { echo "FAIL: NSG SSH rule child resource missing in infra/main.bicep (Slice 3 T13)" >&2; exit 1; }
grep -q "resource nsgRuleHttpsInbound 'Microsoft.Network/networkSecurityGroups/securityRules" infra/main.bicep \
    || { echo "FAIL: NSG HTTPS rule child resource missing in infra/main.bicep (Slice 3 T13)" >&2; exit 1; }

# Confirm NSG itself does NOT declare securityRules as a property (regression guard).
if awk '/^resource nsg /,/^}$/' infra/main.bicep | grep -q "securityRules:"; then
    echo "FAIL: NSG declares inline securityRules — must be child resources (Slice 3 T13, Contrarian P0 in deploy-ssh-jit.md)" >&2
    exit 1
fi

# Slice 3 T14: Network Contributor on ghOidcMi scoped to NSG only
grep -q "var roleDefIdNetworkContributor = subscriptionResourceId" infra/main.bicep \
    || { echo "FAIL: roleDefIdNetworkContributor variable missing (Slice 3 T14)" >&2; exit 1; }
grep -q "resource ghOidcNsgContributorAssignment 'Microsoft.Authorization/roleAssignments" infra/main.bicep \
    || { echo "FAIL: ghOidcNsgContributorAssignment missing (Slice 3 T14)" >&2; exit 1; }
grep -q "scope: nsg" infra/main.bicep \
    || { echo "FAIL: ghOidc Network Contributor must be scoped to nsg (NOT subscription/RG)" >&2; exit 1; }

# Slice 3 nsgName output (consumed by deploy.yml + reaper)
grep -q "^output nsgName string = nsg.name" infra/main.bicep \
    || { echo "FAIL: nsgName output missing (consumed by deploy workflows)" >&2; exit 1; }

echo "=== Slice 4 grep assertions ==="

# Slice 4 T07: Reader on RG for VM MI (was Slice 3 manual patch).
grep -q "resource vmMiReaderAssignment 'Microsoft.Authorization/roleAssignments" infra/main.bicep \
    || { echo "FAIL: vmMiReaderAssignment missing (Slice 4 T07 — IaC parity)" >&2; exit 1; }
grep -q "var roleDefIdReader = subscriptionResourceId" infra/main.bicep \
    || { echo "FAIL: roleDefIdReader variable missing (Slice 4 T07)" >&2; exit 1; }

# Slice 4 T08: storage lifecycle policy.
grep -q "resource backupsLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies" infra/main.bicep \
    || { echo "FAIL: backupsLifecycle resource missing (Slice 4 T08)" >&2; exit 1; }
grep -q "name: 'default'" infra/main.bicep \
    || { echo "FAIL: lifecycle policy must be named 'default' (singleton — research §5)" >&2; exit 1; }
grep -q "msai-backups/backup-" infra/main.bicep \
    || { echo "FAIL: lifecycle prefixMatch must be 'msai-backups/backup-' (research §5)" >&2; exit 1; }
grep -q "daysAfterCreationGreaterThan: 30" infra/main.bicep \
    || { echo "FAIL: 30-day retention rule missing in lifecycle policy" >&2; exit 1; }

# Slice 4 T10: alerts module reference.
grep -q "module alerts './alerts.bicep'" infra/main.bicep \
    || { echo "FAIL: alerts module reference missing (Slice 4 T10)" >&2; exit 1; }

# Slice 4: Bicep loads + base64-encodes new Slice 4 systemd units into cloud-init.
grep -q "loadTextContent('../scripts/backup-to-blob.service')" infra/main.bicep \
    || { echo "FAIL: backup-to-blob.service loadTextContent missing" >&2; exit 1; }
grep -q "__SLICE4_BICEP_BASE64_OF_BACKUP_TIMER__" infra/main.bicep \
    || { echo "FAIL: backup timer cloud-init placeholder not substituted" >&2; exit 1; }

echo "Slice 2/3/4 grep assertions clean."

if [[ "${SKIP_WHATIF:-}" == "1" ]] || ! az account show >/dev/null 2>&1; then
    echo "Skipping what-if (no Azure auth or SKIP_WHATIF=1)."
    exit 0
fi

OPERATOR_IP="${TEST_OPERATOR_IP:-0.0.0.0}"
OPERATOR_PRINCIPAL_ID="${TEST_OPERATOR_PRINCIPAL_ID:-00000000-0000-0000-0000-000000000000}"
SSH_PUBKEY="${TEST_SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDummyKeyForCIWhatIfOnlyDoNotDeploy CI}"

echo "=== az deployment group what-if (msaiv2_rg) ==="
if ! az deployment group what-if \
    -g msaiv2_rg \
    -f infra/main.bicep \
    --parameters infra/main.bicepparam \
    --parameters "operatorIp=$OPERATOR_IP" \
                 "operatorPrincipalId=$OPERATOR_PRINCIPAL_ID" \
                 "vmSshPublicKey=$SSH_PUBKEY" \
    --no-pretty-print >/tmp/whatif.json; then
    echo "what-if: az command failed — see error above" >&2
    exit 1
fi

# Code-review iter 2 P2 #31 fix: validate JSON parses before treating absence
# of Create/Delete as PASS — `jq -e` exits non-zero on both parse-failure AND
# no-match, so we must check parseability first.
if ! jq -e '.changes' /tmp/whatif.json >/dev/null 2>&1; then
    echo "what-if: output not parseable JSON or missing .changes array" >&2
    exit 1
fi

# Only fail on Delete operations. Create (first run) is expected; Modify (subsequent runs
# with Azure-added default fields) is normal and doesn't violate idempotency.
if jq -e '.changes[] | select(.changeType == "Delete")' /tmp/whatif.json >/dev/null; then
    echo "what-if: unexpected Delete operation. Review and adjust Bicep." >&2
    jq '.changes[] | select(.changeType == "Delete")' /tmp/whatif.json >&2
    exit 1
fi
echo "what-if: no Delete operations. Pass."
