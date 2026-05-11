// MSAI v2 — Deployment Pipeline Slice 1: foundational Azure IaC.
//
// Provisions the resources msaiv2 needs to deploy in subsequent slices:
//   * Networking: VNet/subnet/NSG/Public IP
//   * Storage:    Premium SSD data disk + Standard_LRS storage account + msai-backups blob container
//   * Registry:   Azure Container Registry (Basic SKU)
//   * Secrets:    Azure Key Vault (RBAC mode) + diagnostic settings → Log Analytics
//   * Observability: Log Analytics workspace + Azure Monitor Agent + heartbeat DCR
//   * Identity:   GH OIDC user-assigned MI + federated credential (declared in S1, AcrPush in S2)
//                 VM system-assigned MI with KV/ACR/Blob role assignments
//                 Operator KV Secrets Officer assignment (data-plane access for seeding secrets)
//
// References:
//   docs/decisions/deployment-pipeline-architecture.md (locked)
//   docs/decisions/deployment-pipeline-slicing.md (locked)
//   docs/research/2026-05-09-deploy-pipeline-iac-foundation.md (research brief)
//   docs/plans/2026-05-09-deploy-pipeline-iac-foundation.md (plan, 6 plan-review iterations)

targetScope = 'resourceGroup'

// ─────────────────────────────────────────────────────────────────────────────
// Parameters
// ─────────────────────────────────────────────────────────────────────────────

@description('Azure region for all resources. Must match the resource group location.')
param location string = resourceGroup().location

@description('Linux VM admin username.')
@minLength(3)
@maxLength(32)
param vmAdminUsername string = 'msaiadmin'

@description('SSH public key for the VM admin user. Required at deploy time (no default). Operator passes via deploy script.')
@secure()
param vmSshPublicKey string

@description('Operator IPv4 address allowed for inbound SSH on the NSG (single /32). Required at deploy time.')
param operatorIp string

@description('Operator Entra ID object ID. Used to grant Key Vault Secrets Officer (data-plane) so the operator can seed/rotate secrets. Get via: `az ad signed-in-user show --query id -o tsv`.')
param operatorPrincipalId string

@description('GitHub repository owner (for the OIDC federated credential subject claim).')
param repoOwner string = 'marketsignal'

@description('GitHub repository name (for the OIDC federated credential subject claim).')
param repoName string = 'msai-v2'

@description('GitHub branch the federated credential will accept tokens from. Slice 1 binds to main; Slice 3 may add an environment-bound second credential.')
param repoBranch string = 'main'

@description('Tags applied to every resource.')
param tags object = {
  project: 'msaiv2'
  slice: 'slice-1'
  managedBy: 'bicep'
}

// ─────────────────────────────────────────────────────────────────────────────
// Variables — naming
// Globally-unique resources use uniqueString(rg.id) per research brief topic 7.
// RG-scoped resources use deterministic plain names.
// ─────────────────────────────────────────────────────────────────────────────

var storageAccountName = 'msaibk${uniqueString(resourceGroup().id)}'
var acrName = 'msaiacr${uniqueString(resourceGroup().id)}'
var keyVaultName = 'msai-kv-${uniqueString(resourceGroup().id)}'
var logWorkspaceName = 'msai-law-${uniqueString(resourceGroup().id)}'

var vmName = 'msai-vm'
var vmComputerName = 'msaiv2-vm'
var nicName = 'msai-nic'
var nsgName = 'msai-nsg'
var pipName = 'msai-pip'
var vnetName = 'msai-vnet'
var subnetName = 'msai-subnet'
var dataDiskName = 'msai-data-disk'
var ghOidcMiName = 'msai-gh-oidc'
var heartbeatDcrName = 'msai-heartbeat-dcr'

// Built-in role definition GUIDs (subscription-scoped resourceIds).
// Reference: https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles
var roleDefIdKvSecretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var roleDefIdKvSecretsOfficer = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
var roleDefIdAcrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var roleDefIdAcrPush = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
var roleDefIdBlobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
// Network Contributor — Slice 3 grants this to the GH OIDC MI scoped to the NSG only,
// so the deploy workflow can `az network nsg rule create/delete` for the just-in-time
// transient SSH allow rule (`gha-transient-${run_id}-${run_attempt}`). Built-in role.
// Custom role with only securityRules/{read,write,delete} is a Phase 2 hardening; see
// docs/decisions/deploy-ssh-jit.md "Deferred hardening".
var roleDefIdNetworkContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4d97b98b-1d4f-4787-a291-c67834d212e7')
// Reader — Slice 4 grants this to the VM system-assigned MI scoped to the RG so
// backup-to-blob.sh can read Bicep outputs (storage account name + container)
// via `az deployment group show`. Was granted manually during Slice 3 first-deploy;
// landing in IaC for parity.
var roleDefIdReader = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')

// ─────────────────────────────────────────────────────────────────────────────
// T8 (declared early): GH OIDC user-assigned managed identity + federated credential.
// Slice 1 declared both; Slice 2 added the AcrPush role assignment below (`ghOidcAcrPushAssignment`).
// ─────────────────────────────────────────────────────────────────────────────

resource ghOidcMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: ghOidcMiName
  location: location
  tags: tags
}

resource ghOidcCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: ghOidcMi
  name: 'gh-actions-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${repoOwner}/${repoName}:ref:refs/heads/${repoBranch}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T2: Network — VNet + Subnet + Public IP + NSG
// ─────────────────────────────────────────────────────────────────────────────

// NSG is declared with NO inline `securityRules:` property. Rules below are CHILD
// resources so transient deploy-time rules (e.g. `gha-transient-<run_id>-<attempt>`
// added by .github/workflows/deploy.yml) survive future `az deployment group create`
// reapplies. Inline rules are a complete property — ARM reconciles them as a set on
// every apply, which would silently delete transient rules mid-deploy. Council Plan-
// Review Iter 1, Contrarian P0; see docs/decisions/deploy-ssh-jit.md.
resource nsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: nsgName
  location: location
  tags: tags
  properties: {}
}

resource nsgRuleSshFromOperator 'Microsoft.Network/networkSecurityGroups/securityRules@2024-01-01' = {
  parent: nsg
  name: 'AllowSshFromOperator'
  properties: {
    description: 'SSH from operator workstation (single /32). Tighten if operator IP changes.'
    protocol: 'Tcp'
    sourcePortRange: '*'
    destinationPortRange: '22'
    sourceAddressPrefix: '${operatorIp}/32'
    destinationAddressPrefix: '*'
    access: 'Allow'
    priority: 100
    direction: 'Inbound'
  }
}

resource nsgRuleHttpInbound 'Microsoft.Network/networkSecurityGroups/securityRules@2024-01-01' = {
  parent: nsg
  name: 'AllowHttpInbound'
  properties: {
    description: 'HTTP from anywhere (TLS termination via Caddy on the VM).'
    protocol: 'Tcp'
    sourcePortRange: '*'
    destinationPortRange: '80'
    sourceAddressPrefix: '*'
    destinationAddressPrefix: '*'
    access: 'Allow'
    priority: 110
    direction: 'Inbound'
  }
}

resource nsgRuleHttpsInbound 'Microsoft.Network/networkSecurityGroups/securityRules@2024-01-01' = {
  parent: nsg
  name: 'AllowHttpsInbound'
  properties: {
    description: 'HTTPS from anywhere.'
    protocol: 'Tcp'
    sourcePortRange: '*'
    destinationPortRange: '443'
    sourceAddressPrefix: '*'
    destinationAddressPrefix: '*'
    access: 'Allow'
    priority: 120
    direction: 'Inbound'
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: '10.0.0.0/24'
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-01-01' = {
  name: pipName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 4
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T3: Storage account + Blob backup container
// ─────────────────────────────────────────────────────────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {}
}

resource backupsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: 'msai-backups'
  properties: {
    publicAccess: 'None'
  }
}

// Slice 4: lifecycle policy — auto-delete backup-* blobs older than 30 days.
// Singleton resource named `default` (Azure-mandated — no other name accepted per
// Slice 4 research §5 finding 1). `daysAfterCreationGreaterThan` (not modification)
// because blobs are immutable once written by backup-to-blob.sh. prefixMatch is
// `<container>/<blob-prefix>` not just blob prefix.
resource backupsLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-backup-blobs-30d'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'msai-backups/backup-'
              ]
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterCreationGreaterThan: 30
                }
              }
            }
          }
        }
      ]
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T4: Azure Container Registry (Basic SKU, no admin user — only OIDC for push)
// ─────────────────────────────────────────────────────────────────────────────

resource acr 'Microsoft.ContainerRegistry/registries@2025-04-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T5: Log Analytics workspace + Key Vault + diagnostic settings
// (Log Analytics declared first because both KV diagnostics and AMA target it.)
// ─────────────────────────────────────────────────────────────────────────────

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // enablePurgeProtection is OMITTED (not set to false) — Azure rejects explicit
    // `false` with "cannot be set to false. Enabling the purge protection for a vault
    // is an irreversible action." The property defaults to disabled when absent;
    // explicitly setting `true` is irreversible. Phase 1 stays default (disabled) so
    // RG-nuke + redeploy is possible. Phase 2 may set `true` once the deploy stabilizes.
    sku: {
      family: 'A'
      name: 'standard'
    }
    networkAcls: {
      defaultAction: 'Allow'  // Slice 4 will tighten with VM-IP allowlist or Private Endpoint
      bypass: 'AzureServices'
    }
    publicNetworkAccess: 'Enabled'
  }
}

resource keyVaultDiagSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: keyVault   // EXTENSION pattern, NOT child via parent:
  name: 'kvDiagnostics'
  properties: {
    workspaceId: logWorkspace.id
    logs: [
      {
        category: 'AuditEvent'
        enabled: true
      }
      {
        category: 'AzurePolicyEvaluationDetails'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T6: VM + system-assigned managed identity + Premium SSD data disk
// + cloud-init customData (Docker install, data disk format/mount, render-env script + service)
// ─────────────────────────────────────────────────────────────────────────────

resource dataDisk 'Microsoft.Compute/disks@2024-03-02' = {
  name: dataDiskName
  location: location
  tags: tags
  sku: {
    name: 'Premium_LRS'
  }
  properties: {
    creationData: {
      createOption: 'Empty'
    }
    diskSizeGB: 128
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-01-01' = {
  name: nicName
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: '${vnet.id}/subnets/${subnetName}'
          }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

// Cloud-init customData — see infra/cloud-init.yaml. Bicep loads the script + service file
// (single source of truth) and base64-encodes them into the YAML's write_files.encoding=b64
// slots, sidestepping YAML indentation issues. Also threads vmAdminUsername into usermod.
var cloudInitText = loadTextContent('cloud-init.yaml')
var renderScriptText = loadTextContent('../scripts/render-env-from-kv.sh')
var renderUnitText = loadTextContent('../scripts/msai-render-env.service')
// Slice 4: three additional files baked into cloud-init for fresh provisions.
var backupServiceText = loadTextContent('../scripts/backup-to-blob.service')
var backupTimerText = loadTextContent('../scripts/backup-to-blob.timer')
var installAzcopyText = loadTextContent('../scripts/install-azcopy.sh')
var cloudInit = replace(
  replace(
    replace(
      replace(
        replace(
          replace(cloudInitText, '__SLICE1_BICEP_BASE64_OF_RENDER_SCRIPT__', base64(renderScriptText)),
          '__SLICE1_BICEP_BASE64_OF_UNIT__', base64(renderUnitText)
        ),
        '__SLICE4_BICEP_BASE64_OF_BACKUP_SERVICE__', base64(backupServiceText)
      ),
      '__SLICE4_BICEP_BASE64_OF_BACKUP_TIMER__', base64(backupTimerText)
    ),
    '__SLICE4_BICEP_BASE64_OF_INSTALL_AZCOPY__', base64(installAzcopyText)
  ),
  '__SLICE1_BICEP_VM_ADMIN_USERNAME__', vmAdminUsername
)

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      // Code-review iter 3 P2 #34 fix: Standard_D4s_v6 is Dsv6 family (no `d`).
      // The documented MarketSignal2 quota is on Ddsv6 (D...d...sv6) which is the
      // 'D-series with disk', sized D4ds_v6. Switch to D4ds_v6 to match quota.
      vmSize: 'Standard_D4ds_v6'  // Ddsv6 family (4 vCPU, 16 GB RAM, local temp disk)
    }
    osProfile: {
      computerName: vmComputerName
      adminUsername: vmAdminUsername
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${vmAdminUsername}/.ssh/authorized_keys'
              keyData: vmSshPublicKey
            }
          ]
        }
      }
      customData: base64(cloudInit)
    }
    storageProfile: {
      imageReference: {
        // Canonical changed the URN scheme for 24.04 — older `0001-com-ubuntu-server-*`
        // pattern (used through 22.04) does not exist for Noble. Verified at first deploy
        // against eastus2: `Canonical:ubuntu-24_04-lts:server:latest` resolves to a Gen2
        // image (hyperVGeneration: V2). Available SKUs in this offer: cvm, minimal,
        // server, ubuntu-pro (and -arm64, -gen1 variants). `server` is x64 + Gen2 default.
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
        diskSizeGB: 64
      }
      dataDisks: [
        {
          lun: 0
          createOption: 'Attach'
          managedDisk: {
            id: dataDisk.id
          }
        }
      ]
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
        }
      ]
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T7: Azure Monitor Agent (AMA) + Data Collection Rule (heartbeat) + DCR Association
// AMA does NOT auto-create the DCR — explicit DCR + association is required for any data flow.
// ─────────────────────────────────────────────────────────────────────────────

// First-deploy fix (caught at acceptance smoke step 3): the original DCR was
// `kind`-less with stream `Microsoft-Heartbeat`. AMA's MCS endpoint returned
// 404 ("VM is not associated with the DCR") on every refresh, and Heartbeat
// never flowed to the workspace. Root cause:
//   1. `kind: 'Linux'` is required for AMA to recognize the DCR for Linux VMs.
//   2. `Microsoft-Heartbeat` is NOT a valid AMA stream — AMA emits Heartbeat
//      IMPLICITLY when associated with a properly-kinded DCR with at least one
//      valid data source.
// Fix: declare `kind: 'Linux'` + a minimal `Microsoft-Syslog` data source
// (Warning+ severity on common facilities). Heartbeat then flows automatically
// AND we get useful syslog data for Slice 4 alert rules at no extra cost.
resource heartbeatDcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' = {
  name: heartbeatDcrName
  location: location
  tags: tags
  kind: 'Linux'
  properties: {
    dataSources: {
      syslog: [
        {
          name: 'syslogBase'
          streams: [
            'Microsoft-Syslog'
          ]
          facilityNames: [
            'auth'
            'authpriv'
            'cron'
            'daemon'
            'kern'
            'syslog'
            'user'
          ]
          logLevels: [
            'Warning'
            'Error'
            'Critical'
            'Alert'
            'Emergency'
          ]
        }
      ]
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: logWorkspace.id
          name: 'msaiLogAnalytics'
        }
      ]
    }
    dataFlows: [
      {
        streams: [
          'Microsoft-Syslog'
        ]
        destinations: [
          'msaiLogAnalytics'
        ]
      }
    ]
  }
}

resource amaExtension 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'AzureMonitorLinuxAgent'
  location: location
  tags: tags
  properties: {
    publisher: 'Microsoft.Azure.Monitor'
    type: 'AzureMonitorLinuxAgent'
    typeHandlerVersion: '1.21'
    autoUpgradeMinorVersion: true
    enableAutomaticUpgrade: true
  }
}

resource heartbeatDcrAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2024-03-11' = {
  scope: vm
  name: '${heartbeatDcrName}-association'
  properties: {
    dataCollectionRuleId: heartbeatDcr.id
    description: 'Heartbeat DCR association for msai-vm'
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// T8 (continued): Role assignments
// 5 total: VM gets KV Secrets User + AcrPull + Blob Contributor (3 runtime grants),
// operator gets KV Secrets Officer (1 data-plane grant for seeding/rotating secrets),
// gh-oidc MI gets AcrPush (Slice 2 — CI image push from GitHub Actions).
// ─────────────────────────────────────────────────────────────────────────────

resource vmKvSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(vm.id, keyVault.id, 'kv-secrets-user')
  properties: {
    principalId: vm.identity.principalId
    roleDefinitionId: roleDefIdKvSecretsUser
    principalType: 'ServicePrincipal'
    description: 'VM system-assigned MI reads secrets from KV via render-env-from-kv.sh at boot'
  }
}

resource vmAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(vm.id, acr.id, 'acr-pull')
  properties: {
    principalId: vm.identity.principalId
    roleDefinitionId: roleDefIdAcrPull
    principalType: 'ServicePrincipal'
    description: 'VM system-assigned MI pulls images from ACR for docker compose pull'
  }
}

resource vmBlobContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: backupsContainer
  name: guid(vm.id, backupsContainer.id, 'blob-contributor')
  properties: {
    principalId: vm.identity.principalId
    roleDefinitionId: roleDefIdBlobContributor
    principalType: 'ServicePrincipal'
    description: 'VM system-assigned MI writes nightly backups to msai-backups container (Slice 4 cron)'
  }
}

// Slice 4 IaC parity: Reader on RG for VM MI so backup-to-blob.sh can read Bicep
// outputs via `az deployment group show`. Was granted manually during Slice 3 first
// deploy (CHANGELOG Slice 3 manual-patch list); landing in IaC for parity.
resource vmMiReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(vm.id, resourceGroup().id, 'reader')
  properties: {
    principalId: vm.identity.principalId
    roleDefinitionId: roleDefIdReader
    principalType: 'ServicePrincipal'
    description: 'Slice 4: Reader on RG so backup-to-blob.sh resolves Bicep outputs via az deployment group show. Granted manually during Slice 3 first-deploy; declarative parity.'
  }
}

resource operatorKvSecretsOfficerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(operatorPrincipalId, keyVault.id, 'kv-secrets-officer')
  properties: {
    principalId: operatorPrincipalId
    roleDefinitionId: roleDefIdKvSecretsOfficer
    description: 'Operator (Pablo) data-plane access: seed and rotate secrets in KV. Required because enableRbacAuthorization=true means subscription Owner alone is insufficient.'
  }
}

resource ghOidcAcrPushAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(ghOidcMi.id, acr.id, 'acr-push')
  properties: {
    principalId: ghOidcMi.properties.principalId
    roleDefinitionId: roleDefIdAcrPush
    principalType: 'ServicePrincipal'
    description: 'Slice 2: GitHub Actions OIDC user-assigned MI pushes images to ACR via .github/workflows/build-and-push.yml'
  }
}

// Slice 3: Network Contributor on NSG ONLY (not RG, not subscription). The deploy
// workflow opens a transient SSH allow rule for the runner's public IP just-in-time,
// then deletes it in a separate cleanup job. A leaked rule is reaped by
// .github/workflows/reap-orphan-nsg-rules.yml on a 15-min cron. Council-mandated
// (Plan-Review Iter 1, Contrarian P0 — see docs/decisions/deploy-ssh-jit.md).
resource ghOidcNsgContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: nsg
  name: guid(ghOidcMi.id, nsg.id, 'network-contributor')
  properties: {
    principalId: ghOidcMi.properties.principalId
    roleDefinitionId: roleDefIdNetworkContributor
    principalType: 'ServicePrincipal'
    description: 'Slice 3: GH OIDC MI mutates NSG security rules to open transient SSH for deploy.yml. Scoped to this NSG only — defense-in-depth via Phase 2 Azure Policy deferred per docs/decisions/deploy-ssh-jit.md.'
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Slice 4: Observability module — Action Group, App Insights, 4 alert rules,
// availability test. See infra/alerts.bicep header for design rationale.
// ─────────────────────────────────────────────────────────────────────────────

module alerts './alerts.bicep' = {
  name: 'alerts-${uniqueString(resourceGroup().id)}'
  params: {
    location: location
    logAnalyticsWorkspaceId: logWorkspace.id
    operatorEmail: 'pablo@ksgai.com'
    msaiHostname: 'platform.marketsignal.ai'
    tags: tags
  }
}

// Outputs (consumed by Slice 2/3 via `az deployment group show --query 'properties.outputs'`)
// ─────────────────────────────────────────────────────────────────────────────

output acrLoginServer string = acr.properties.loginServer
output keyVaultUri string = keyVault.properties.vaultUri
output keyVaultName string = keyVault.name
output vmPublicIp string = publicIp.properties.ipAddress
output ghOidcClientId string = ghOidcMi.properties.clientId
output vmPrincipalId string = vm.identity.principalId
output logAnalyticsWorkspaceId string = logWorkspace.properties.customerId
output backupsStorageAccount string = storageAccount.name
output backupsContainerName string = backupsContainer.name
output nsgName string = nsg.name
output actionGroupId string = alerts.outputs.actionGroupId
output appInsightsId string = alerts.outputs.appInsightsId

