#!/usr/bin/env bash
# MSAI v2 — Azure deployment script (Bicep-driven; Slice 1 onward).
#
# Replaces the legacy `az vm create` flow that targeted msai-rg / eastus / D4s_v5.
# Slice 1+ uses Bicep IaC: infra/main.bicep deployed to msaiv2_rg / eastus2 / D4ds_v6.
#
# Usage:
#   ./scripts/deploy-azure.sh                  # deploy infra/main.bicep to msaiv2_rg
#   ./scripts/deploy-azure.sh --what-if        # dry-run (no changes applied)
#   ./scripts/deploy-azure.sh --help
#
# Environment overrides:
#   OPERATOR_IP   IPv4 to allowlist for SSH on the NSG. Defaults to current
#                 public IPv4 via `curl -4 -fsS https://api.ipify.org`.
#
# Pre-flight (operator must run before invoking this script):
#   az login --tenant 2237d332-fc65-4994-b676-61edad7be319    # MarketSignal tenant
#   az account set --subscription MarketSignal2

set -euo pipefail

readonly EXPECTED_SUB_ID="68067b9b-943f-4461-8cb5-2bc97cbc462d"  # MarketSignal2
readonly RG="msaiv2_rg"
readonly LOCATION="eastus2"
readonly TEMPLATE="infra/main.bicep"
readonly PARAM_FILE="infra/main.bicepparam"

usage() {
    cat <<EOF
Usage: $0 [--what-if|--help]
  (no flag)    Deploy $TEMPLATE to $RG ($LOCATION)
  --what-if    Run az deployment group what-if (dry-run; no changes applied)
  --help       Show this message

Environment:
  OPERATOR_IP  IPv4 to allowlist for SSH on the NSG. Defaults to current
               public IPv4 via 'curl -4 -fsS https://api.ipify.org' if not set.
EOF
}

preflight() {
    local mode="${1:-create}"  # 'create' or 'whatif'

    # Code-review iter 2 P2: check template path FIRST (cheap, no Azure I/O, no side effects).
    if [[ ! -f "$TEMPLATE" ]]; then
        echo "Template not found: $TEMPLATE" >&2
        echo "Run from repo root." >&2
        exit 1
    fi

    local current_sub
    current_sub=$(az account show --query 'id' -o tsv 2>/dev/null || true)
    if [[ -z "$current_sub" ]]; then
        echo "az not authenticated. Run: az login" >&2
        exit 1
    fi
    if [[ "$current_sub" != "$EXPECTED_SUB_ID" ]]; then
        echo "Wrong subscription. Run: az account set --subscription $EXPECTED_SUB_ID" >&2
        echo "(Currently on: $current_sub)" >&2
        exit 1
    fi

    # Code-review iter 2 P2 #29 fix: --what-if must NOT create the RG. Dry-runs are
    # supposed to be read-only. If the RG is missing in --what-if mode, fail and tell
    # the operator to invoke without --what-if first.
    if ! az group show -n "$RG" >/dev/null 2>&1; then
        if [[ "$mode" == "whatif" ]]; then
            echo "Resource group '$RG' does not exist. --what-if is read-only and will not create it." >&2
            echo "Run without --what-if first to create the RG, OR create it manually:" >&2
            echo "  az group create -n $RG -l $LOCATION" >&2
            exit 1
        fi
        echo "Resource group '$RG' missing. Creating in $LOCATION..."
        az group create -n "$RG" -l "$LOCATION" -o none
    fi

    # Code-review iter 2 P2 #32 fix: warn about soft-deleted Key Vault reservation.
    # Bicep names KV deterministically via uniqueString(rg.id), and KV soft-delete
    # reserves the name for 90 days even with enablePurgeProtection=false. After an
    # RG nuke + recreate, deploying the same KV name fails until the soft-deleted
    # vault is purged. We don't compute the expected name here (would require duplicating
    # the uniqueString logic), but we surface the issue if any soft-deleted vault matches
    # the msai-kv- prefix.
    local soft_deleted
    soft_deleted=$(az keyvault list-deleted --query "[?starts_with(name, 'msai-kv-')].name" -o tsv 2>/dev/null || true)
    if [[ -n "$soft_deleted" ]]; then
        echo "WARNING: Soft-deleted Key Vault(s) match 'msai-kv-' prefix:" >&2
        echo "$soft_deleted" >&2
        echo "If this deploy targets the same vault name, it will FAIL with KeyVaultAlreadyExists." >&2
        echo "To purge: az keyvault purge --name <vault-name>" >&2
        echo "To recover: az keyvault recover --name <vault-name>" >&2
        echo "(Continuing — the deploy may still succeed if the deterministic vault name doesn't collide.)" >&2
    fi
}

resolve_operator_ip() {
    # Code-review iter 1 P2 #26 fix: force IPv4 (-4) and validate format.
    # Code-review iter 2 P2 #30 fix: use HTTPS (api.ipify.org) instead of plaintext
    # http://ifconfig.me — a MITM on the unencrypted endpoint could return an
    # arbitrary IPv4 that passes the regex but locks the operator out of SSH.
    local ip="${OPERATOR_IP:-}"
    if [[ -z "$ip" ]]; then
        ip=$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)
    fi
    if [[ ! "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        echo "Operator IP '$ip' is not a valid IPv4 address." >&2
        echo "Pass an explicit IPv4 via OPERATOR_IP=<x.x.x.x>." >&2
        exit 1
    fi
    printf '%s' "$ip"
}

# Load operator's SSH public key. Prefer modern ed25519, fall back to RSA / ECDSA.
resolve_ssh_public_key() {
    local key=""
    for candidate in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub" "$HOME/.ssh/id_ecdsa.pub"; do
        if [[ -f "$candidate" ]]; then
            key=$(<"$candidate")
            break
        fi
    done
    if [[ -z "$key" ]]; then
        echo "No SSH public key found in ~/.ssh/{id_ed25519,id_rsa,id_ecdsa}.pub" >&2
        echo "Generate one: ssh-keygen -t ed25519 -C msaiv2" >&2
        exit 1
    fi
    printf '%s' "$key"
}

# Resolve the Entra object ID of the signed-in user. Required so the Bicep can grant
# Key Vault Secrets Officer (data-plane) — without this, KV RBAC blocks even the
# subscription Owner from `az keyvault secret set/show`.
resolve_operator_principal_id() {
    local pid
    pid=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
    if [[ -z "$pid" ]]; then
        echo "Could not resolve signed-in user object ID." >&2
        echo "Are you logged in as a user (not a service principal)?" >&2
        exit 1
    fi
    printf '%s' "$pid"
}

deploy_bicep_create() {
    local operator_ip operator_pid ssh_pubkey
    operator_ip=$(resolve_operator_ip)
    operator_pid=$(resolve_operator_principal_id)
    ssh_pubkey=$(resolve_ssh_public_key)
    echo "Operator IP: $operator_ip"
    echo "Operator principal ID: $operator_pid"
    echo "SSH key: ${ssh_pubkey:0:30}..."
    echo "Deploying $TEMPLATE to $RG (create)..."
    az deployment group create \
        --resource-group "$RG" \
        --template-file "$TEMPLATE" \
        --parameters "$PARAM_FILE" \
        --parameters operatorIp="$operator_ip" \
                     operatorPrincipalId="$operator_pid" \
                     vmSshPublicKey="$ssh_pubkey" \
        --query "{status: properties.provisioningState, correlationId: properties.correlationId}" \
        -o table
}

deploy_bicep_whatif() {
    local operator_ip operator_pid ssh_pubkey
    operator_ip=$(resolve_operator_ip)
    operator_pid=$(resolve_operator_principal_id)
    ssh_pubkey=$(resolve_ssh_public_key)
    echo "Operator IP: $operator_ip"
    echo "Operator principal ID: $operator_pid"
    echo "What-if $TEMPLATE against $RG..."
    # `az deployment group what-if` does NOT accept --query/-o table — it has its own
    # output renderer.
    az deployment group what-if \
        --resource-group "$RG" \
        --template-file "$TEMPLATE" \
        --parameters "$PARAM_FILE" \
        --parameters operatorIp="$operator_ip" \
                     operatorPrincipalId="$operator_pid" \
                     vmSshPublicKey="$ssh_pubkey"
}

main() {
    case "${1:-}" in
        --help|-h)
            usage
            exit 0
            ;;
        --what-if)
            preflight whatif
            deploy_bicep_whatif
            ;;
        "")
            preflight create
            deploy_bicep_create
            ;;
        *)
            echo "Unknown flag: $1" >&2
            usage
            exit 1
            ;;
    esac
}

main "$@"
