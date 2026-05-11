// MSAI v2 — Slice 4 Observability module.
//
// Resources:
//   * Action Group `msai-ops-alerts` (email pablo@ksgai.com)
//   * Application Insights `msai-app-insights` (workspace-based; required for
//     the standard availability test since URL-ping retires 2026-09-30 per Slice 4
//     research §6 finding 1)
//   * Availability test `msai-health-ping` (kind: 'standard', 3 geo-locations
//     to stay <€10/mo per research §6 finding 3, mandatory hidden-link tag)
//   * 4 scheduledQueryRules — pinned to API version 2022-06-15 GA (research §2:
//     2023-12-01-preview is unregistered in many regions and breaks deploy)
//
// All 4 alerts route to the single `msai-ops-alerts` action group. Severities:
// 1=critical (health, backup), 2=warning (orphan NSG rule, container restart heuristic).
//
// Slice 4 research findings driving design:
//   §1 — n/a here (azcopy)
//   §2 — pin scheduledQueryRules @2022-06-15 (NOT 2023-12-01-preview)
//   §3 — container restart KQL is a HEURISTIC over the Syslog stream (Container
//        Insights is K8s-only; AMA on a VM doesn't emit per-container metrics)
//   §4 — n/a here (systemd)
//   §5 — n/a here (storage lifecycle in main.bicep)
//   §6 — App Insights component + kind='standard' + 3 geoLocations + hidden-link tag
//   §7 — n/a here (Caddy passthrough)

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string

@description('Slice 1 Log Analytics workspace resource ID — App Insights links here.')
param logAnalyticsWorkspaceId string

@description('Operator email — receives all alerts. Single-operator project so single receiver.')
param operatorEmail string

@description('Fully-qualified production hostname for the availability test.')
param msaiHostname string = 'platform.marketsignal.ai'

@description('Resource tags inherited from main.bicep.')
param tags object

// ─────────────────────────────────────────────────────────────────────────────
// Action Group
// ─────────────────────────────────────────────────────────────────────────────

resource actionGroup 'Microsoft.Insights/actionGroups@2024-10-01-preview' = {
  name: 'msai-ops-alerts'
  // Action Groups MUST live in 'global'; using `location` would deploy-fail.
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'msai-ops'
    enabled: true
    emailReceivers: [
      {
        name: 'operator-email'
        emailAddress: operatorEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Application Insights (workspace-based) + Availability Test
// ─────────────────────────────────────────────────────────────────────────────

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'msai-app-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    // Workspace-based — classic AI is being retired. Logs flow into the existing
    // Slice 1 Log Analytics workspace so all observability data lives in one place.
    WorkspaceResourceId: logAnalyticsWorkspaceId
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource availabilityTest 'Microsoft.Insights/webtests@2022-06-15' = {
  name: 'msai-health-ping'
  location: location
  // Mandatory `hidden-link` tag per research §6 finding 4 — Azure portal uses this
  // to render the availability test under the App Insights blade. Without it the
  // resource works but the portal won't display it.
  tags: union(tags, {
    'hidden-link:${appInsights.id}': 'Resource'
  })
  kind: 'standard'
  properties: {
    SyntheticMonitorId: 'msai-health-ping'
    Name: 'msai-health-ping'
    Description: 'Slice 4: external HTTPS probe against /health. Five-minute interval, 3 geo-locations to stay <€10/mo per research §6.'
    Enabled: true
    Frequency: 300 // 5 min
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      // Three locations across continents — research §6 finding 3 (<€10/mo).
      { Id: 'us-il-ch1-azr' }    // North Central US
      { Id: 'us-ca-sjc-azr' }    // West US
      { Id: 'emea-nl-ams-azr' }  // North Europe
    ]
    Request: {
      RequestUrl: 'https://${msaiHostname}/health'
      HttpVerb: 'GET'
      ParseDependentRequests: false
    }
    ValidationRules: {
      ExpectedHttpStatusCode: 200
      SSLCheck: true
      SSLCertRemainingLifetimeCheck: 7
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Scheduled Query Rules — pinned to 2022-06-15 GA (research §2)
// ─────────────────────────────────────────────────────────────────────────────

// Alert 1: /health availability failed (App Insights availability results).
resource alertHealthAvailability 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'msai-health-availability-alert'
  location: location
  tags: tags
  properties: {
    displayName: '/health availability failed'
    description: 'Slice 4: external availability test of https://${msaiHostname}/health returned non-success in last 5 min.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      appInsights.id
    ]
    targetResourceTypes: [
      'Microsoft.Insights/components'
    ]
    criteria: {
      allOf: [
        {
          query: 'availabilityResults | where success == 0 | where timestamp > ago(5m)'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 2 // 2 failed probes in 5 min ≈ 2 distinct locations failed; reduces single-location flake
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    autoMitigate: true
  }
}

// Alert 2: backup-to-blob.service failed in last 24h (Syslog stream).
resource alertBackupFailure 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'msai-backup-failure-alert'
  location: location
  tags: tags
  properties: {
    displayName: 'backup-to-blob.service failed'
    description: 'Slice 4: nightly backup-to-blob.service exited non-zero within last 24h.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT1H'
    windowSize: 'P1D'
    scopes: [
      logAnalyticsWorkspaceId
    ]
    targetResourceTypes: [
      'Microsoft.OperationalInsights/workspaces'
    ]
    criteria: {
      allOf: [
        {
          // Code-review P1: broaden match — rsyslog forwarders often strip the
          // `.service` suffix or use the unit basename; systemd's own failure
          // events ("Failed with result", "Main process exited", "exit-code")
          // are sometimes emitted by ProcessName=systemd not by the service. Match
          // anywhere in SyslogMessage AND alternate process names. Slice 4
          // acceptance smoke (docs/runbooks/slice-4-acceptance.md Step 2) tests
          // this query against actual prod journald output — adjust if it misses.
          query: 'Syslog | where SyslogMessage has_any ("backup-to-blob.service", "backup-to-blob[") | where SyslogMessage has_any ("Failed with result", "Main process exited", "exit-code", "ERROR:") | where TimeGenerated > ago(1d)'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    autoMitigate: false // Manual ack — backup failures need an operator decision
  }
}

// Alert 3: orphan gha-transient-* NSG rule >30 min (AzureActivity stream).
// Tracks ssh-jit rule creates without a matching delete within 30 min. A leaked
// transient rule is a real risk after Slice 3 cleanup-job failures (see
// docs/decisions/deploy-ssh-jit.md).
resource alertOrphanNsgRule 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'msai-orphan-nsg-rule-alert'
  location: location
  tags: tags
  properties: {
    displayName: 'Orphan transient NSG rule >30 min'
    description: 'Slice 4: gha-transient-* NSG rule created >30 min ago and not yet deleted (reap-orphan-nsg-rules.yml should catch within 15 min; this alert fires if both the deploy-cleanup job AND the reaper failed).'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT15M'
    // Window must be longer than the longest credible "rule leaked but
    // operator hasn't noticed" duration. PR #58 Codex round-4 P2: PT1H was
    // too short — a rule created >1h ago falls outside the query window and
    // the alert auto-resolves while the rule is still exposed. PT24H gives
    // 24-hour visibility before the create-event ages out; if a rule is leaked
    // longer than that, the every-15-min reaper would have caught it many
    // times over (or the operator notices via the Slice 4 acceptance
    // procedure's quarterly drift-check).
    windowSize: 'PT24H'
    scopes: [
      logAnalyticsWorkspaceId
    ]
    targetResourceTypes: [
      'Microsoft.OperationalInsights/workspaces'
    ]
    criteria: {
      allOf: [
        {
          query: '''
            AzureActivity
            | where OperationNameValue endswith "securityRules/write"
            | where ActivityStatusValue == "Success"
            | extend rule_name = tostring(split(Properties_d.entity, "/")[-1])
            | where rule_name startswith "gha-transient-"
            | where TimeGenerated < ago(30m)
            | join kind=leftanti (
                AzureActivity
                | where OperationNameValue endswith "securityRules/delete"
                | where ActivityStatusValue == "Success"
                | extend rule_name = tostring(split(Properties_d.entity, "/")[-1])
                | where rule_name startswith "gha-transient-"
            ) on rule_name
          '''
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    autoMitigate: true
  }
}

// Alert 4: container restart HEURISTIC (Syslog stream).
// Research §3 finding 1: AMA on a VM doesn't emit per-container metrics — Container
// Insights is K8s-only. So this is a heuristic, NOT a true counter. Watches Docker
// daemon logs for restart events in the last 10 min on msai-prefixed containers.
// Tunable threshold (3); revisit if false-positive rate exceeds 1/week.
resource alertContainerRestartHeuristic 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = {
  name: 'msai-container-restart-heuristic'
  location: location
  tags: tags
  properties: {
    displayName: 'Container restart heuristic — msai-* containers'
    description: 'Slice 4 (HEURISTIC, not true counter): Syslog stream shows ≥3 docker restart events on msai-* containers in last 10 min. Slice 5+ should migrate to true container metrics if false-positive rate is unacceptable.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [
      logAnalyticsWorkspaceId
    ]
    targetResourceTypes: [
      'Microsoft.OperationalInsights/workspaces'
    ]
    criteria: {
      allOf: [
        {
          // Code-review P2 finding: dockerd's actual restart-related log verbs are
          // "exit"/"exited"/"signal"/"died"/"OOMKilled" — NOT "restarted" (which
          // is more compose-cli phrasing). Match the dockerd literal verbs + the
          // containerd-shim exit messages compose-managed restarts produce. Slice 4
          // research §3 already flagged this as a HEURISTIC; reviewed against prod
          // journald output at Slice 4 acceptance (docs/runbooks/slice-4-acceptance.md).
          query: 'Syslog | where (ProcessName == "dockerd" or ProcessName has "containerd") and SyslogMessage has_any ("exit", "exited", "signal", "OOMKilled", "died") and SyslogMessage has "msai-" | where TimeGenerated > ago(10m)'
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 3
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
    autoMitigate: true
  }
}

output actionGroupId string = actionGroup.id
output appInsightsId string = appInsights.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output availabilityTestId string = availabilityTest.id
