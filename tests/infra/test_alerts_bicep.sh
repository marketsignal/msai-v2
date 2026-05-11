#!/usr/bin/env bash
# CI test for infra/alerts.bicep (Slice 4 observability module).

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== az bicep build infra/alerts.bicep ==="
az bicep build --file infra/alerts.bicep --stdout >/dev/null
echo "Lint clean."

echo "=== Slice 4 alerts grep assertions ==="

# Pin to 2022-06-15 GA. 2023-12-01-preview is unregistered in many regions
# (Slice 4 research §2 finding 2) — would fail at deploy time.
if grep -qE "scheduledQueryRules@2023" infra/alerts.bicep; then
    echo "FAIL: scheduledQueryRules must pin to 2022-06-15 GA (research §2 — 2023-12-01-preview is regionally unregistered)" >&2
    exit 1
fi
grep -q "scheduledQueryRules@2022-06-15" infra/alerts.bicep \
    || { echo "FAIL: scheduledQueryRules@2022-06-15 not present" >&2; exit 1; }

# Exactly 4 alert resources (health-availability, backup-failure, orphan-nsg-rule, container-restart).
count=$(grep -cE "^resource alert[A-Z]" infra/alerts.bicep || echo 0)
if [[ "$count" != "4" ]]; then
    echo "FAIL: expected exactly 4 alert resources, found $count" >&2
    exit 1
fi

# Action Group must be at 'global' location.
if ! awk '/^resource actionGroup /,/^}$/' infra/alerts.bicep | grep -q "location: 'global'"; then
    echo "FAIL: actionGroup must be location: 'global' (Azure-mandated)" >&2
    exit 1
fi

# Operator email present in module input.
grep -q "operatorEmail" infra/alerts.bicep \
    || { echo "FAIL: operatorEmail param missing" >&2; exit 1; }

# Availability test: kind: 'standard' + exactly 3 geoLocations + hidden-link tag.
grep -q "kind: 'standard'" infra/alerts.bicep \
    || { echo "FAIL: availability test must be kind: 'standard' (URL-ping retires 2026-09-30)" >&2; exit 1; }
loc_count=$(grep -cE "^      \{ Id: '" infra/alerts.bicep || echo 0)
if [[ "$loc_count" -gt 3 ]]; then
    echo "FAIL: availability test has $loc_count geoLocations; limit to 3 to stay <€10/mo (research §6)" >&2
    exit 1
fi
grep -q "hidden-link" infra/alerts.bicep \
    || { echo "FAIL: hidden-link tag missing (required for portal rendering)" >&2; exit 1; }

# App Insights must be workspace-based (link to Slice 1 Log Analytics).
grep -q "WorkspaceResourceId: logAnalyticsWorkspaceId" infra/alerts.bicep \
    || { echo "FAIL: AppInsights must be workspace-based (classic AI is being retired)" >&2; exit 1; }

# Container restart alert documented as HEURISTIC.
grep -q -i "heuristic" infra/alerts.bicep \
    || { echo "FAIL: container restart alert must be documented as heuristic (Slice 4 research §3)" >&2; exit 1; }

echo "alerts.bicep tests passed."
