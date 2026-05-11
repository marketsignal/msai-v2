// MSAI v2 — Slice 4 P2 fix from Codex PR #58 review.
//
// Wires Azure Activity Log → Log Analytics workspace so the
// `msai-orphan-nsg-rule-alert` KQL query against `AzureActivity` actually
// returns rows. Without this, the alert silently misses orphan-rule events
// because the table is empty.
//
// SCOPE: subscription. The diagnosticSettings resource type for Activity Log
// must be deployed at subscription scope, NOT resource group scope. Main.bicep
// (RG-scoped) references this module via `module … = { scope: subscription() }`.
//
// PERMISSIONS: requires subscription-level Owner OR Monitoring Contributor on
// the subscription to apply. Pablo has Owner per Slice 1 acceptance, so the
// re-apply runbook (docs/runbooks/iac-parity-reapply.md) can land this.
//
// COST: Activity Log ingestion is free for the first 5GB/month. MSAI v2's
// activity volume is well under that (~tens of events/day at Phase 1).

targetScope = 'subscription'

@description('Log Analytics workspace resource ID — from main.bicep logWorkspace.id.')
param logAnalyticsWorkspaceId string

@description('Diagnostic-setting name — singleton-ish per workspace.')
param settingName string = 'msai-activity-log-to-law'

resource activityLogDiagnostic 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: settingName
  scope: subscription()
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    // `allLogs` is the 2026 idiom and forwards every activity-log category
    // (Administrative, Security, ServiceHealth, Alert, Recommendation, Policy,
    // Autoscale, ResourceHealth). Slice 4's orphan-NSG-rule alert needs the
    // Administrative category specifically (network/securityRules write/delete).
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

output diagnosticSettingId string = activityLogDiagnostic.id
