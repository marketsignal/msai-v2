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
