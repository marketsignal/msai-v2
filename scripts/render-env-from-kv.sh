#!/usr/bin/env bash
# MSAI v2 — boot-time secret renderer.
#
# Called by msai-render-env.service. Fetches secrets from Azure Key Vault via the VM's
# system-assigned managed identity and writes /run/msai.env (mode 0600). The output file
# is consumed by docker-compose-msai.service (wired in Slice 3).
#
# Required env (set in service unit ExecStart):
#   KV_NAME             — Key Vault short name (e.g., msai-kv-abc123def4)
#   REQUIRED_SECRETS    — comma-separated env-var-style names; renderer fails on missing
#   OPTIONAL_SECRETS    — (optional) comma-separated; logs + skips on missing
#
# Behavior:
#   - 10-attempt IMDS retry with exponential backoff (research brief topic 5: doubled
#     from the standard 5-attempt guidance to absorb post-deployment MI propagation tail
#     of 30-90s with outliers to 3-5min).
#   - 5-attempt KV retry per secret with linear backoff (~100s budget for RBAC propagation).
#   - 403 from KV = retry (RBAC propagating); 404/401 from KV = fail fast (config wrong).
#   - Atomic write via /run/msai.env.tmp → mv. Single quotes escape values for compose
#     and systemd EnvironmentFile parsers.
#
# Secret-leak protection:
#   - /run/msai.env is mode 0600, owner root:root.
#   - curl -s suppresses curl's progress output.
#   - Response bodies captured in shell variables; no temp files on disk.
#   - Secret values are never echoed to stdout/stderr.

set -euo pipefail

: "${KV_NAME:?KV_NAME must be set}"
: "${REQUIRED_SECRETS:?REQUIRED_SECRETS must be set}"
OPTIONAL_SECRETS="${OPTIONAL_SECRETS:-}"

OUTPUT_FILE="/run/msai.env"
TMP_FILE="/run/msai.env.tmp"
IMDS_URL="http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net"

# Azure Key Vault secret names allow only alphanumerics and `-`. We accept env-var-style
# names (with underscores) in REQUIRED_SECRETS / OPTIONAL_SECRETS, normalize for the KV
# lookup, and write the original underscore name back to /run/msai.env. Mirrors the
# convention already used by backend/src/msai/core/secrets.py:112.
to_kv_name() {
    tr '[:upper:]_' '[:lower:]-' <<<"$1"
}

# Retry IMDS up to 10 times with exponential backoff.
get_token() {
    local attempt resp delay token
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if [[ "$attempt" -eq 1 ]]; then
            delay=0
        elif [[ "$attempt" -le 5 ]]; then
            delay=$((2 ** (attempt - 1)))
        else
            delay=30
        fi
        sleep "$delay"
        if resp=$(curl -sf --max-time 10 -H "Metadata: true" "$IMDS_URL" 2>/dev/null); then
            token=$(jq -r .access_token <<<"$resp" 2>/dev/null || true)
            if [[ -n "$token" && "$token" != "null" ]]; then
                printf '%s' "$token"
                return 0
            fi
        fi
    done
    echo "IMDS token unavailable after 10 attempts" >&2
    return 1
}

# Fetch one secret. Distinguishes retryable (403 = RBAC propagating) from permanent
# (404 = wrong name, 401 = wrong tenant/MI). Captures response body in memory only.
get_secret() {
    local token="$1" env_name="$2"
    local kv_name attempt resp http_code body
    kv_name=$(to_kv_name "$env_name")
    for attempt in 1 2 3 4 5; do
        # 0, 10, 20, 30, 40s = ~100s budget for RBAC propagation
        sleep "$((attempt == 1 ? 0 : 10 * (attempt - 1)))"
        # Capture body + HTTP status in a single curl call. The trailing \n%{http_code}
        # is unambiguous: KV JSON responses don't end with a bare 3-digit numeric line.
        resp=$(curl -s -w '\n%{http_code}' --max-time 10 \
            -H "Authorization: Bearer $token" \
            "https://${KV_NAME}.vault.azure.net/secrets/${kv_name}?api-version=7.4" || true)
        if [[ -z "$resp" ]]; then
            http_code="000"
            body=""
        else
            http_code="${resp##*$'\n'}"
            body="${resp%$'\n'*}"
        fi
        case "$http_code" in
            200)
                jq -r .value <<<"$body"
                return 0
                ;;
            403)
                # RBAC propagating — retry.
                continue
                ;;
            404)
                echo "Secret '$kv_name' (env: $env_name) not found in KV $KV_NAME (404)" >&2
                return 2
                ;;
            401)
                echo "KV auth failed (401) for '$kv_name' — fix MI or KV config" >&2
                return 2
                ;;
            *)
                echo "KV unexpected status $http_code for '$kv_name'" >&2
                ;;
        esac
    done
    return 1
}

# PR-review (Codex bot) P2 fix: emit values in dotenv double-quoted form, NOT shell
# single-quote concat. /run/msai.env is consumed by Docker Compose env-file parsing
# (dotenv format), which does NOT understand shell quoting like `'abc'"'"'def'` —
# verified empirically: shell-style escape produces no value (silent dropout); dotenv
# double-quoted produces correct value. systemd EnvironmentFile is also dotenv-style.
# Reference: https://docs.docker.com/compose/environment-variables/env-file/
emit_kv_line() {
    local key="$1" val="$2"
    # Escape backslashes FIRST (must precede other escapes), then double quotes,
    # then $ (compose interpolates ${VAR} in env-file values otherwise).
    val="${val//\\/\\\\}"
    val="${val//\"/\\\"}"
    val="${val//\$/\\\$}"
    printf '%s="%s"\n' "$key" "$val"
}

main() {
    local token
    token=$(get_token) || exit 1

    : >"$TMP_FILE"
    chmod 600 "$TMP_FILE"

    local -a required=() optional=()
    local secret value

    # REQUIRED secrets — fail hard if any missing.
    IFS=',' read -ra required <<<"$REQUIRED_SECRETS"
    for secret in "${required[@]}"; do
        if value=$(get_secret "$token" "$secret"); then
            emit_kv_line "$secret" "$value" >>"$TMP_FILE"
        else
            echo "Required secret '$secret' missing or unreachable; aborting." >&2
            rm -f "$TMP_FILE"
            exit 1
        fi
    done

    # OPTIONAL secrets — log warning + skip if missing.
    if [[ -n "$OPTIONAL_SECRETS" ]]; then
        IFS=',' read -ra optional <<<"$OPTIONAL_SECRETS"
        for secret in "${optional[@]}"; do
            if value=$(get_secret "$token" "$secret" 2>/dev/null); then
                emit_kv_line "$secret" "$value" >>"$TMP_FILE"
            else
                echo "Optional secret '$secret' not present in KV; skipping." >&2
            fi
        done
    fi

    # Atomic move into place.
    mv "$TMP_FILE" "$OUTPUT_FILE"
    echo "Rendered $OUTPUT_FILE: ${#required[@]} required + ${#optional[@]} optional secrets"
}

main "$@"
